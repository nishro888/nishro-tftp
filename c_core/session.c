#include "session.h"
#include "packet.h"
#include "acl.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* TID allocator: we use the client's ephemeral as their TID, and
 * allocate a fresh server TID from this pool. */
#define SERVER_TID_BASE   40000
#define SERVER_TID_RANGE  20000

/* Maximum active sessions supported by the fixed-size table. The cfg
 * may cap lower. */
#define SESS_MAX          256

#define MAX_RETRANS       5
#define TICK_MS           50
#define PKT_BUF_SZ        2048

typedef enum {
    S_FREE = 0,
    S_OACK_WAIT_ACK0,    /* sent OACK, waiting for ACK(0) */
    S_SEND_RRQ,          /* actively sending DATA for RRQ */
    S_RECV_WRQ,          /* receiving DATA for WRQ */
    S_DONE,              /* transfer complete, emit session_end */
} SState;

typedef struct {
    SState state;
    uint32_t id;

    /* Client context */
    uint8_t  client_mac[6];
    uint32_t client_ip_be;
    uint16_t client_port;
    uint16_t server_port;   /* our TID */
    uint16_t vlan_id;

    /* Transfer */
    int      kind;          /* TFTP_RRQ or TFTP_WRQ */
    char     filename[MAX_PATHLEN];
    FileSource *src;
    uint64_t total_bytes;
    uint64_t bytes_done;

    /* Negotiated options */
    uint16_t blksize;
    uint8_t  windowsize;
    uint16_t timeout_sec;
    uint64_t tsize;
    int      options_negotiated;

    /* Protocol state */
    uint32_t next_block;    /* next block to send (RRQ) or expect (WRQ) */
    uint32_t window_last;   /* last block we've sent so far in window */
    int      eof_reached;   /* RRQ: true once we've read EOF */
    uint64_t window_first_offset;   /* RRQ: file offset of block=next_block */
    uint32_t retrans_count;
    uint64_t next_timeout_ms;

    /* Timing */
    uint64_t started_ms;
    uint64_t last_progress_ms;   /* rate-limit IPC progress events */

    /* Prebuilt next window (RRQ only). Filled immediately after the
     * current window is transmitted, so the ACK path just flushes it.
     * Invalidated on retransmit or any state the prebuild assumed. */
    PcapBatch *prebuilt;
    uint32_t   prebuilt_first_block;
    uint32_t   prebuilt_last_block;
    uint64_t   prebuilt_first_offset;
    uint64_t   prebuilt_end_offset;   /* byte offset after the prebuilt window */
    int        prebuilt_eof;
    int        prebuilt_valid;
} Session;

struct SessMgr {
    const Config *cfg;
    PcapIO *io;
    PcapBatch *batch;            /* reused across every window send */
    Session slots[SESS_MAX];
    uint32_t next_id;
    uint16_t next_server_tid;
    SessStats stats;
};

/* ---------------------------------------------------------------- */

static void ipc_line(const char *line) {
    /* stdout is the IPC channel to Python parent. */
    fputs(line, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

static const char *sstate_name(SState st) {
    switch (st) {
    case S_OACK_WAIT_ACK0: return "negotiating";
    case S_SEND_RRQ:       return "transferring";
    case S_RECV_WRQ:       return "transferring";
    case S_DONE:           return "done";
    default:               return "idle";
    }
}

static void emit_session_start(const Session *s) {
    char ip[16];  ipv4_to_str(s->client_ip_be, ip);
    char mac[18]; mac_to_str(s->client_mac, mac);
    char buf[640];
    snprintf(buf, sizeof buf,
        "{\"ev\":\"session_start\",\"id\":%u,\"kind\":\"%s\","
        "\"filename\":\"%s\","
        "\"client_mac\":\"%s\",\"client_ip\":\"%s\",\"client_port\":%u,"
        "\"vlan_id\":%d,\"blksize\":%u,\"windowsize\":%u,"
        "\"bytes_transferred\":%llu,\"total_bytes\":%llu,"
        "\"state\":\"%s\",\"started_at\":%llu}",
        s->id, s->kind == TFTP_RRQ ? "read" : "write",
        s->filename, mac, ip, (unsigned)s->client_port,
        s->vlan_id == 0xFFFF ? -1 : (int)s->vlan_id,
        (unsigned)s->blksize, (unsigned)s->windowsize,
        (unsigned long long)s->bytes_done,
        (unsigned long long)s->total_bytes,
        sstate_name(s->state),
        (unsigned long long)(s->started_ms / 1000ULL));
    ipc_line(buf);
}

/* Rate-limited progress emit. Every ACK triggers this in the hot path,
 * and each emit does an fflush on the stdout pipe to the Python parent
 * (synchronous Windows WriteFile). 200ms is frequent enough for the UI
 * and keeps the IPC pipe out of the send loop. */
#define PROGRESS_MIN_INTERVAL_MS 200ULL
static void emit_session_progress(Session *s) {
    uint64_t tnow = now_ms();
    int is_final = (s->bytes_done == s->total_bytes && s->total_bytes > 0);
    if (!is_final && s->last_progress_ms &&
        tnow - s->last_progress_ms < PROGRESS_MIN_INTERVAL_MS) {
        return;
    }
    s->last_progress_ms = tnow;
    char buf[256];
    snprintf(buf, sizeof buf,
        "{\"ev\":\"session_progress\",\"id\":%u,"
        "\"bytes_transferred\":%llu,\"total_bytes\":%llu,\"state\":\"%s\"}",
        s->id, (unsigned long long)s->bytes_done,
        (unsigned long long)s->total_bytes, sstate_name(s->state));
    ipc_line(buf);
}

/* Ends a session. ``ok`` = transfer completed cleanly. ``server_fault`` is
 * true only for genuine server-side failures (disk write error, commit
 * failure) -- client dropouts, timeouts, policy rejections and bad
 * client requests are not faults. When !ok the reason string is sent as
 * the ``state`` field itself so the web UI can show the actual cause in
 * the session-history "state" column instead of a generic "failed". */
static void emit_session_end(const Session *s, int ok, int server_fault,
                             const char *reason) {
    uint64_t tnow = now_ms();
    char buf[640];
    const char *state = ok ? "done" : (reason && reason[0] ? reason : "ended");
    snprintf(buf, sizeof buf,
        "{\"ev\":\"session_end\",\"id\":%u,\"ok\":%s,\"server_fault\":%s,"
        "\"bytes_transferred\":%llu,\"total_bytes\":%llu,"
        "\"duration_ms\":%llu,\"ended_at\":%llu,"
        "\"state\":\"%s\"%s%s%s}",
        s->id, ok ? "true" : "false", server_fault ? "true" : "false",
        (unsigned long long)s->bytes_done,
        (unsigned long long)s->total_bytes,
        (unsigned long long)(tnow - s->started_ms),
        (unsigned long long)(tnow / 1000ULL),
        state,
        reason && !ok ? ",\"error\":\"" : "",
        reason && !ok ? reason          : "",
        reason && !ok ? "\""            : "");
    ipc_line(buf);
}

/* ---------------------------------------------------------------- */

static Session *find_or_alloc(SessMgr *m,
                              uint32_t ip_be, uint16_t cport, uint16_t sport) {
    for (int i = 0; i < SESS_MAX; i++) {
        Session *s = &m->slots[i];
        if (s->state == S_FREE) continue;
        if (s->client_ip_be == ip_be && s->client_port == cport &&
            s->server_port == sport) return s;
    }
    return NULL;
}

static Session *alloc_slot(SessMgr *m) {
    uint32_t cap = m->cfg->max_sessions ? m->cfg->max_sessions : SESS_MAX;
    if (cap > SESS_MAX) cap = SESS_MAX;
    if (m->stats.sessions_active >= cap) return NULL;
    for (int i = 0; i < SESS_MAX; i++) {
        if (m->slots[i].state == S_FREE) {
            memset(&m->slots[i], 0, sizeof(m->slots[i]));
            return &m->slots[i];
        }
    }
    return NULL;
}

static void free_slot(SessMgr *m, Session *s) {
    if (!s) return;
    if (s->src) { src_close(s->src); s->src = NULL; }
    if (s->prebuilt) { pcapio_batch_free(s->prebuilt); s->prebuilt = NULL; }
    s->prebuilt_valid = 0;
    if (s->state != S_FREE) m->stats.sessions_active--;
    s->state = S_FREE;
}

/* Send a built UDP packet with TFTP payload using our server TID.
 * `count_bytes` controls whether the payload length is added to
 * bytes_sent -- ERROR packets are protocol signalling and should not
 * show up in the dashboard's throughput totals. */
static void send_tftp_ex(SessMgr *m, const Session *s,
                         const uint8_t *payload, size_t len,
                         int count_bytes) {
    uint8_t buf[PKT_BUF_SZ];
    size_t n = build_udp4(buf, sizeof buf,
                         s->client_mac, m->cfg->virtual_mac,
                         s->vlan_id == 0xFFFF ? 0xFFFF : s->vlan_id,
                         m->cfg->virtual_ip_be, s->client_ip_be,
                         s->server_port, s->client_port,
                         payload, len);
    if (!n) return;
    pcapio_send(m->io, buf, n);
    if (count_bytes) m->stats.bytes_sent += len;
}

static void send_tftp(SessMgr *m, const Session *s,
                      const uint8_t *payload, size_t len) {
    send_tftp_ex(m, s, payload, len, 1);
}

static void send_error(SessMgr *m, Session *s, uint16_t code, const char *msg) {
    uint8_t pkt[512];
    size_t n = tftp_build_error(pkt, sizeof pkt, code, msg);
    send_tftp_ex(m, s, pkt, n, 0);
    m->stats.errors_sent++;
}

/* Apply negotiation policy: for each option the client asked for,
 * clamp into [min,max] from config. */
static void negotiate(const Config *c, TftpReq *r) {
    if (r->has_blksize) {
        if (r->blksize < c->blksize_min) r->blksize = c->blksize_min;
        if (r->blksize > c->blksize_max) r->blksize = c->blksize_max;
    }
    if (r->has_windowsize) {
        if (r->windowsize < c->windowsize_min) r->windowsize = c->windowsize_min;
        if (r->windowsize > c->windowsize_max) r->windowsize = c->windowsize_max;
    }
    if (r->has_timeout) {
        if (r->timeout_sec < c->timeout_min) r->timeout_sec = c->timeout_min;
        if (r->timeout_sec > c->timeout_max) r->timeout_sec = c->timeout_max;
    }
}

/* ---------------------------------------------------------------- */
/* RRQ: read `windowsize` blocks from file and send them as DATA(n..).
 * `start_block` is the first block number to send; file must already
 * be positioned at its offset. */
/* Fill `batch` with DATA packets for a full window starting at `first_block`
 * / `first_offset`. Returns 1 on success, 0 on EOF reached (partial ok),
 * -1 on fatal I/O error. On success, *out_last and *out_end_offset describe
 * what was queued; *out_eof tells if EOF was hit. Does NOT transmit. */
static int rrq_fill_window(SessMgr *m, Session *s, PcapBatch *batch,
                           uint32_t first_block, uint64_t first_offset,
                           uint32_t *out_last, uint64_t *out_end_offset,
                           int *out_eof) {
    uint8_t pkt[PKT_BUF_SZ];
    uint8_t frame[PKT_BUF_SZ];
    uint8_t data[TFTP_BLKSIZE_MAX];
    uint32_t sent = 0;
    uint32_t last = first_block ? (first_block - 1) : 0;
    uint64_t off = first_offset;
    int eof = 0;
    if (src_seek(s->src, first_offset) < 0) return -1;
    while (sent < s->windowsize) {
        int n = src_read(s->src, data, s->blksize);
        if (n < 0) return -1;
        uint32_t block = first_block + sent;
        uint16_t block16 = (uint16_t)block;
        size_t plen = tftp_build_data(pkt, sizeof pkt, block16, data, (size_t)n);
        if (!plen) return -1;
        size_t fn = build_udp4(frame, sizeof frame,
                               s->client_mac, m->cfg->virtual_mac,
                               s->vlan_id == 0xFFFF ? 0xFFFF : s->vlan_id,
                               m->cfg->virtual_ip_be, s->client_ip_be,
                               s->server_port, s->client_port,
                               pkt, plen);
        if (!fn) return -1;
        if (pcapio_batch_add(batch, frame, fn) < 0) break;  /* batch full */
        last = block;
        off += (uint32_t)n;
        sent++;
        if ((uint32_t)n < s->blksize) { eof = 1; break; }
    }
    *out_last = last;
    *out_end_offset = off;
    *out_eof = eof;
    return 1;
}

/* Prebuild the NEXT window while we wait for an ACK on the current one.
 * This overlaps disk reads + packet construction with network RTT, so
 * the ACK path becomes a single pcap_sendqueue_transmit call. */
static void rrq_prebuild_next(SessMgr *m, Session *s) {
    if (s->eof_reached || !s->src) return;
    if (!s->prebuilt) return;
    uint32_t first = s->window_last + 1;
    uint64_t off   = s->window_first_offset +
                     (uint64_t)(s->window_last + 1 - s->next_block) * s->blksize;
    uint32_t last;
    uint64_t end_off;
    int eof;
    if (rrq_fill_window(m, s, s->prebuilt, first, off,
                        &last, &end_off, &eof) != 1) {
        s->prebuilt_valid = 0;
        return;
    }
    s->prebuilt_first_block  = first;
    s->prebuilt_last_block   = last;
    s->prebuilt_first_offset = off;
    s->prebuilt_end_offset   = end_off;
    s->prebuilt_eof          = eof;
    s->prebuilt_valid        = 1;
}

/* Transmit the current window (from disk) then prebuild the next. */
static void rrq_send_window(SessMgr *m, Session *s) {
    if (!s->src) return;
    /* Invalidate any stale prebuild: we're starting fresh from disk. */
    s->prebuilt_valid = 0;
    uint32_t last;
    uint64_t end_off;
    int eof;
    int rv = rrq_fill_window(m, s, m->batch, s->next_block,
                             s->window_first_offset,
                             &last, &end_off, &eof);
    if (rv < 0) {
        pcapio_batch_flush(m->io, m->batch);
        send_error(m, s, TFTP_ERR_ACCESS, "read error");
        s->state = S_DONE;
        return;
    }
    s->window_last  = last;
    s->eof_reached  = eof;
    m->stats.bytes_sent += (end_off - s->window_first_offset);
    pcapio_batch_flush(m->io, m->batch);
    s->next_timeout_ms = now_ms() + (uint64_t)s->timeout_sec * 1000ULL;
    /* Preload the next window for the ACK fast-path. */
    rrq_prebuild_next(m, s);
}

/* On ACK(N): advance next_block to N+1, update file offset, send next
 * window. If eof was reached and N == window_last, we're done. */
static void rrq_on_ack(SessMgr *m, Session *s, uint16_t ack_block) {
    /* ack_block is 16-bit; map back to 32-bit space using next_block/
     * window_last as an anchor. Choose candidate closest to the window
     * we've emitted. */
    uint32_t cand = (s->window_last & 0xFFFF0000u) | ack_block;
    if (cand > s->window_last)      cand -= 0x10000;
    else if (cand + 0x10000 <= s->window_last) cand += 0x10000;

    if (cand < s->next_block) {
        /* stale ack */
        return;
    }
    /* Advance file offset to reflect the number of blocks now
     * acknowledged. */
    uint64_t advanced = (uint64_t)(cand + 1 - s->next_block) * s->blksize;
    s->bytes_done = s->window_first_offset + advanced;
    if (s->bytes_done > s->total_bytes) s->bytes_done = s->total_bytes;

    if (s->eof_reached && cand >= s->window_last) {
        emit_session_progress(s);
        emit_session_end(s, 1, 0, NULL);
        m->stats.sessions_completed++;
        s->state = S_DONE;
        return;
    }

    s->next_block = cand + 1;
    s->window_first_offset = s->bytes_done;
    s->retrans_count = 0;

    /* Fast path: the client ACKed the entire window we transmitted and
     * we have the next one already built in memory. Flush it in ONE
     * kernel call, then prebuild the one after that. No disk I/O, no
     * packet construction between ACK and transmit. */
    if (s->prebuilt_valid &&
        s->prebuilt_first_block == s->next_block &&
        cand == s->window_last) {
        s->window_last          = s->prebuilt_last_block;
        s->eof_reached          = s->prebuilt_eof;
        m->stats.bytes_sent    += (s->prebuilt_end_offset - s->prebuilt_first_offset);
        pcapio_batch_flush(m->io, s->prebuilt);
        s->next_timeout_ms = now_ms() + (uint64_t)s->timeout_sec * 1000ULL;
        s->prebuilt_valid = 0;
        emit_session_progress(s);
        rrq_prebuild_next(m, s);
        return;
    }

    /* Slow path: partial ACK, rollover anchor drift, or no prebuild --
     * rebuild from disk. */
    s->prebuilt_valid = 0;
    emit_session_progress(s);
    rrq_send_window(m, s);
}

/* ---------------------------------------------------------------- */
/* WRQ: we ACK(0) after OACK (or the initial WRQ if no options), then
 * collect DATA(1..N). On last short block, ACK then commit file. */
static void wrq_send_ack(SessMgr *m, Session *s, uint16_t block) {
    uint8_t pkt[32];
    size_t n = tftp_build_ack(pkt, sizeof pkt, block);
    send_tftp(m, s, pkt, n);
    s->next_timeout_ms = now_ms() + (uint64_t)s->timeout_sec * 1000ULL;
}

static void wrq_on_data(SessMgr *m, Session *s,
                        uint16_t block, const uint8_t *payload, size_t len) {
    /* Rollover: map block into 32-bit space using next_block anchor. */
    uint32_t cand = (s->next_block & 0xFFFF0000u) | block;
    if (cand + 0x10000 <= s->next_block) cand += 0x10000;
    else if (cand > s->next_block + 0x8000) cand -= 0x10000;

    if (cand < s->next_block) {
        /* Duplicate -- re-ACK and discard. */
        wrq_send_ack(m, s, block);
        return;
    }
    if (cand > s->next_block) {
        /* Out of order -- drop, wait for retransmit. */
        return;
    }
    /* In-order block. */
    if (m->cfg->max_wrq_size &&
        s->bytes_done + len > m->cfg->max_wrq_size) {
        /* Client-submitted upload is over the server policy cap. Policy
         * rejection, not a server fault. */
        send_error(m, s, TFTP_ERR_DISK_FULL, "over size limit");
        s->state = S_DONE;
        emit_session_end(s, 0, 0, "exceeds size limit");
        return;
    }
    if (s->src && src_write(s->src, payload, len) < 0) {
        /* Real disk write failure -- genuine server fault. */
        send_error(m, s, TFTP_ERR_DISK_FULL, "write failed");
        s->state = S_DONE;
        m->stats.sessions_failed++;
        emit_session_end(s, 0, 1, "disk write failed");
        return;
    }
    s->bytes_done += len;
    s->next_block = cand + 1;
    s->retrans_count = 0;
    wrq_send_ack(m, s, block);
    emit_session_progress(s);

    if (len < s->blksize) {
        /* last block -- finalize */
        int rv = s->src ? src_commit_write(s->src) : -1;
        s->src = NULL;
        if (rv < 0) {
            /* Commit failure (FTP upload, disk rename) -- server fault. */
            m->stats.sessions_failed++;
            emit_session_end(s, 0, 1, "write commit failed");
        } else {
            m->stats.sessions_completed++;
            emit_session_end(s, 1, 0, NULL);
        }
        s->state = S_DONE;
    }
}

/* ---------------------------------------------------------------- */

static void start_rrq(SessMgr *m, Session *s, TftpReq *req) {
    /* Seed blksize/windowsize up front so failed-early snapshots (file
     * not found, size-limit rejection, etc.) report a meaningful value
     * instead of 0. negotiate() may later clamp these to policy bounds,
     * overwriting them below before emit_session_start runs on the
     * happy path. */
    s->blksize      = req->has_blksize    ? (uint16_t)req->blksize    : TFTP_BLKSIZE_DEFAULT;
    s->windowsize   = req->has_windowsize ? (uint8_t )req->windowsize : TFTP_WINDOWSIZE_DEFAULT;
    s->timeout_sec  = req->has_timeout    ? (uint16_t)req->timeout_sec: TFTP_TIMEOUT_DEFAULT;

    uint64_t sz = 0;
    s->src = src_open_read(req->filename, &sz);
    if (!s->src) {
        /* Client asked for a file we don't have -- not a server fault. */
        send_error(m, s, TFTP_ERR_FILE_NOT_FOUND, "not found");
        s->state = S_DONE;
        emit_session_start(s);
        emit_session_end(s, 0, 0, "file not found");
        return;
    }
    s->total_bytes = sz;
    if (m->cfg->max_rrq_size && sz > m->cfg->max_rrq_size) {
        /* Server policy cap -- policy rejection, not a fault. */
        send_error(m, s, TFTP_ERR_ACCESS, "over size limit");
        s->state = S_DONE;
        emit_session_start(s);
        emit_session_end(s, 0, 0, "exceeds size limit");
        return;
    }

    /* Negotiate options. */
    negotiate(m->cfg, req);
    if (req->has_tsize) { req->tsize = sz; }   /* server fills tsize */

    s->blksize      = req->has_blksize    ? (uint16_t)req->blksize    : TFTP_BLKSIZE_DEFAULT;
    s->windowsize   = req->has_windowsize ? (uint8_t )req->windowsize : TFTP_WINDOWSIZE_DEFAULT;
    s->timeout_sec  = req->has_timeout    ? (uint16_t)req->timeout_sec: TFTP_TIMEOUT_DEFAULT;
    s->tsize        = sz;
    s->options_negotiated = req->has_blksize | req->has_windowsize |
                            req->has_timeout | req->has_tsize;

    emit_session_start(s);

    /* Allocate prebuild batch: one full window's worth of frames.
     * windowsize * (blksize + L2/L3/L4 headers). 128 KiB covers
     * windowsize=64 @ blksize=1468 with headroom. */
    if (!s->prebuilt) s->prebuilt = pcapio_batch_new(128 * 1024);

    if (s->options_negotiated) {
        uint8_t pkt[512];
        size_t n = tftp_build_oack(pkt, sizeof pkt, req);
        send_tftp(m, s, pkt, n);
        s->state = S_OACK_WAIT_ACK0;
        s->next_timeout_ms = now_ms() + (uint64_t)s->timeout_sec * 1000ULL;
    } else {
        /* No options -- start sending DATA at block 1 immediately. */
        s->state = S_SEND_RRQ;
        s->next_block = 1;
        s->window_first_offset = 0;
        rrq_send_window(m, s);
    }
}

static void start_wrq(SessMgr *m, Session *s, TftpReq *req) {
    /* Same early seed as start_rrq -- see comment there. */
    s->blksize      = req->has_blksize    ? (uint16_t)req->blksize    : TFTP_BLKSIZE_DEFAULT;
    s->windowsize   = req->has_windowsize ? (uint8_t )req->windowsize : TFTP_WINDOWSIZE_DEFAULT;
    s->timeout_sec  = req->has_timeout    ? (uint16_t)req->timeout_sec: TFTP_TIMEOUT_DEFAULT;

    if (!m->cfg->wrq_enabled) {
        /* Policy rejection -- admin disabled writes. Not a server fault. */
        send_error(m, s, TFTP_ERR_ACCESS, "wrq disabled");
        s->state = S_DONE;
        emit_session_start(s);
        emit_session_end(s, 0, 0, "writes disabled");
        return;
    }
    s->src = src_open_write(req->filename);
    if (!s->src) {
        /* Path traversal / invalid target -- client/policy rejection. */
        send_error(m, s, TFTP_ERR_ACCESS, "write denied");
        s->state = S_DONE;
        emit_session_start(s);
        emit_session_end(s, 0, 0, "write denied");
        return;
    }

    negotiate(m->cfg, req);
    if (req->has_tsize && m->cfg->max_wrq_size && req->tsize > m->cfg->max_wrq_size) {
        /* Policy rejection -- client declared upload is too large. */
        send_error(m, s, TFTP_ERR_ACCESS, "over size limit");
        s->state = S_DONE;
        emit_session_start(s);
        emit_session_end(s, 0, 0, "exceeds size limit");
        return;
    }

    s->blksize      = req->has_blksize    ? (uint16_t)req->blksize    : TFTP_BLKSIZE_DEFAULT;
    s->windowsize   = req->has_windowsize ? (uint8_t )req->windowsize : TFTP_WINDOWSIZE_DEFAULT;
    s->timeout_sec  = req->has_timeout    ? (uint16_t)req->timeout_sec: TFTP_TIMEOUT_DEFAULT;
    s->total_bytes  = req->has_tsize ? req->tsize : 0;
    s->options_negotiated = req->has_blksize | req->has_windowsize |
                            req->has_timeout | req->has_tsize;

    emit_session_start(s);

    if (s->options_negotiated) {
        uint8_t pkt[512];
        size_t n = tftp_build_oack(pkt, sizeof pkt, req);
        send_tftp(m, s, pkt, n);
    } else {
        wrq_send_ack(m, s, 0);
    }
    s->state = S_RECV_WRQ;
    s->next_block = 1;
}

/* ---------------------------------------------------------------- */

int sessmgr_on_tftp(SessMgr *m,
                    const uint8_t client_mac[6], uint32_t client_ip_be,
                    uint16_t client_port, uint16_t server_port,
                    uint16_t vlan_id,
                    const uint8_t *tftp, size_t tftp_len) {
    if (tftp_len < 2) return -1;
    uint16_t op = (uint16_t)((tftp[0] << 8) | tftp[1]);

    if (op == TFTP_RRQ || op == TFTP_WRQ) {
        if (!acl_allowed(m->cfg, vlan_id == 0xFFFF ? 0 : vlan_id,
                         client_ip_be, SVC_TFTP)) {
            m->stats.acl_denied++;
            return 0;
        }
        TftpReq req;
        if (tftp_parse_request(tftp, tftp_len, &req) < 0) return -1;

        Session *s = alloc_slot(m);
        if (!s) {
            /* No capacity -- respond with ERROR. Build a throwaway view. */
            uint8_t pkt[64];
            size_t n = tftp_build_error(pkt, sizeof pkt, TFTP_ERR_NOT_DEFINED, "server busy");
            uint8_t buf[PKT_BUF_SZ];
            size_t bn = build_udp4(buf, sizeof buf,
                                  client_mac, m->cfg->virtual_mac,
                                  vlan_id, m->cfg->virtual_ip_be, client_ip_be,
                                  (uint16_t)(SERVER_TID_BASE + (m->next_server_tid++ % SERVER_TID_RANGE)),
                                  client_port, pkt, n);
            if (bn) pcapio_send(m->io, buf, bn);
            return 0;
        }
        s->id = ++m->next_id;
        memcpy(s->client_mac, client_mac, 6);
        s->client_ip_be = client_ip_be;
        s->client_port  = client_port;
        s->server_port  = (uint16_t)(SERVER_TID_BASE + (m->next_server_tid++ % SERVER_TID_RANGE));
        s->vlan_id      = vlan_id;
        s->kind         = op;
        s->started_ms   = now_ms();
        str_copy(s->filename, req.filename, sizeof s->filename);
        m->stats.sessions_total++;
        m->stats.sessions_active++;
        if (op == TFTP_RRQ) m->stats.rrq_count++; else m->stats.wrq_count++;

        if (op == TFTP_RRQ) start_rrq(m, s, &req);
        else                start_wrq(m, s, &req);
        return 0;
    }

    /* Non-request ops must match an existing session (addressed to our
     * server TID). */
    Session *s = find_or_alloc(m, client_ip_be, client_port, server_port);
    if (!s) return 0;

    if (op == TFTP_ACK && tftp_len >= 4) {
        uint16_t ack = (uint16_t)((tftp[2] << 8) | tftp[3]);
        if (s->state == S_OACK_WAIT_ACK0) {
            if (ack == 0) {
                if (s->kind == TFTP_RRQ) {
                    s->state = S_SEND_RRQ;
                    s->next_block = 1;
                    s->window_first_offset = 0;
                    rrq_send_window(m, s);
                } else {
                    /* WRQ already in S_RECV_WRQ after OACK path -- won't reach */
                }
            }
            return 0;
        }
        if (s->state == S_SEND_RRQ) rrq_on_ack(m, s, ack);
        return 0;
    }

    if (op == TFTP_DATA && tftp_len >= 4 && s->state == S_RECV_WRQ) {
        uint16_t block = (uint16_t)((tftp[2] << 8) | tftp[3]);
        m->stats.bytes_received += tftp_len - 4;
        wrq_on_data(m, s, block, tftp + 4, tftp_len - 4);
        return 0;
    }

    if (op == TFTP_ERROR) {
        /* Client sent an ERROR packet (aborted from its side) -- not our
         * fault, just record the reason. */
        emit_session_end(s, 0, 0, "client aborted");
        s->state = S_DONE;
        return 0;
    }

    return 0;
}

/* ---------------------------------------------------------------- */

void sessmgr_tick(SessMgr *m) {
    uint64_t tnow = now_ms();
    for (int i = 0; i < SESS_MAX; i++) {
        Session *s = &m->slots[i];
        if (s->state == S_FREE) continue;
        if (s->state == S_DONE) { free_slot(m, s); continue; }
        if (s->next_timeout_ms && tnow >= s->next_timeout_ms) {
            if (++s->retrans_count > MAX_RETRANS) {
                /* Client went silent -- client-side dropout, not a fault. */
                emit_session_end(s, 0, 0, "client timeout");
                free_slot(m, s);
                continue;
            }
            /* Retransmit current window. */
            if (s->state == S_OACK_WAIT_ACK0) {
                /* OACK retransmit -- rebuild from fresh TftpReq is tricky.
                 * Simplest: just resend whatever window we had. For
                 * OACK_WAIT_ACK0 we haven't sent data yet, so just bump
                 * the timeout; client should retry its RRQ. */
                s->next_timeout_ms = tnow + (uint64_t)s->timeout_sec * 1000ULL;
            } else if (s->state == S_SEND_RRQ) {
                /* Retransmit: the prebuilt batch represents the NEXT
                 * window, not this one, so invalidate and rebuild from
                 * disk. rrq_send_window will re-prebuild after. */
                s->prebuilt_valid = 0;
                rrq_send_window(m, s);
            } else if (s->state == S_RECV_WRQ) {
                /* Re-ACK the last block we accepted. */
                if (s->next_block > 1) wrq_send_ack(m, s, (uint16_t)(s->next_block - 1));
                else                    wrq_send_ack(m, s, 0);
            }
        }
    }
}

SessMgr *sessmgr_new(const Config *cfg, PcapIO *io) {
    SessMgr *m = calloc(1, sizeof(*m));
    if (!m) return NULL;
    m->cfg = cfg;
    m->io = io;
    /* One batch per mgr. Sized for the worst case: max windowsize *
     * (max blksize + Ethernet/IP/UDP/TFTP headers). 64 * 2048 = 128K,
     * comfortably covers windowsize_max 64 with headroom. */
    m->batch = pcapio_batch_new(128 * 1024);
    if (!m->batch) { free(m); return NULL; }
    return m;
}

void sessmgr_free(SessMgr *m) {
    if (!m) return;
    for (int i = 0; i < SESS_MAX; i++)
        if (m->slots[i].state != S_FREE) free_slot(m, &m->slots[i]);
    if (m->batch) pcapio_batch_free(m->batch);
    free(m);
}

void sessmgr_config_changed(SessMgr *m, const Config *cfg) { m->cfg = cfg; }

void sessmgr_get_stats(const SessMgr *m, SessStats *o) { *o = m->stats; }
