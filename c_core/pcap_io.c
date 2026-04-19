#include "pcap_io.h"
#include "util.h"
#include <pcap.h>
#include <string.h>
#include <stdlib.h>

struct PcapIO {
    pcap_t *p;
};

PcapIO *pcapio_open(const char *device, int promiscuous, char *errbuf_256) {
    char eb[PCAP_ERRBUF_SIZE];
    /* Use the create/activate API so we can crank the kernel ring buffer
     * before activating. snaplen 2048 covers blksize 1468 + headers. */
    pcap_t *p = pcap_create(device, eb);
    if (!p) {
        if (errbuf_256) str_copy(errbuf_256, eb, 256);
        return NULL;
    }
    pcap_set_snaplen(p, 2048);
    pcap_set_promisc(p, promiscuous ? 1 : 0);
    /* Read timeout 1ms -- keep the dispatch loop responsive. */
    pcap_set_timeout(p, 1);
    /* 16 MiB kernel ring buffer. Npcap default is ~1 MiB which overflows
     * under sustained high-rate transfers. */
    pcap_set_buffer_size(p, 16 * 1024 * 1024);
    if (pcap_activate(p) != 0) {
        if (errbuf_256) str_copy(errbuf_256, pcap_geterr(p), 256);
        pcap_close(p);
        return NULL;
    }
    /* Minimum-copy: return immediately on first packet, don't batch. */
    pcap_setmintocopy(p, 0);
    PcapIO *h = calloc(1, sizeof(*h));
    if (!h) { pcap_close(p); return NULL; }
    h->p = p;
    return h;
}

int pcapio_set_filter(PcapIO *h, const char *filter) {
    if (!filter || !*filter) return 0;
    struct bpf_program prog;
    if (pcap_compile(h->p, &prog, filter, 1, PCAP_NETMASK_UNKNOWN) < 0) {
        LOGE("pcap_compile failed: %s", pcap_geterr(h->p));
        return -1;
    }
    int r = pcap_setfilter(h->p, &prog);
    pcap_freecode(&prog);
    if (r < 0) {
        LOGE("pcap_setfilter failed: %s", pcap_geterr(h->p));
        return -1;
    }
    return 0;
}

int pcapio_next(PcapIO *h, int timeout_ms,
                const uint8_t **out_buf, size_t *out_len) {
    /* pcap_open_live already applies its own read timeout (1ms above).
     * We loop until our budget expires so the caller sees at least
     * timeout_ms of patience even if pcap returns 0 early. */
    uint64_t deadline = now_ms() + (uint64_t)timeout_ms;
    for (;;) {
        struct pcap_pkthdr *hdr;
        const uint8_t *data;
        int r = pcap_next_ex(h->p, &hdr, &data);
        if (r == 1) {
            *out_buf = data;
            *out_len = hdr->caplen;
            return 1;
        }
        if (r < 0) {
            LOGE("pcap_next_ex error: %s", pcap_geterr(h->p));
            return -1;
        }
        /* r == 0 -> timeout within pcap; loop until our own deadline */
        if (now_ms() >= deadline) return 0;
    }
}

typedef struct { pcapio_cb cb; void *ud; int count; } DispatchCtx;

static void _dispatch_trampoline(u_char *user, const struct pcap_pkthdr *hdr,
                                 const u_char *data) {
    DispatchCtx *c = (DispatchCtx *)user;
    c->cb((const uint8_t *)data, (size_t)hdr->caplen, c->ud);
    c->count++;
}

int pcapio_dispatch(PcapIO *h, int max_packets, pcapio_cb cb, void *ud) {
    DispatchCtx c = { .cb = cb, .ud = ud, .count = 0 };
    /* cnt=-1 with non-blocking pcap_dispatch returns all currently
     * buffered packets then returns 0. */
    int r = pcap_dispatch(h->p, max_packets, _dispatch_trampoline, (u_char *)&c);
    if (r < 0) return -1;
    return c.count;
}

int pcapio_send(PcapIO *h, const uint8_t *buf, size_t len) {
    return pcap_sendpacket(h->p, buf, (int)len);
}

/* --- batched send via pcap_sendqueue ------------------------------- */
struct PcapBatch {
    pcap_send_queue *q;
    size_t cap;
};

PcapBatch *pcapio_batch_new(size_t capacity_bytes) {
    PcapBatch *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    b->q = pcap_sendqueue_alloc((u_int)capacity_bytes);
    if (!b->q) { free(b); return NULL; }
    b->cap = capacity_bytes;
    return b;
}

int pcapio_batch_add(PcapBatch *b, const uint8_t *buf, size_t len) {
    struct pcap_pkthdr hdr;
    hdr.ts.tv_sec = 0;
    hdr.ts.tv_usec = 0;
    hdr.caplen = (bpf_u_int32)len;
    hdr.len    = (bpf_u_int32)len;
    if (pcap_sendqueue_queue(b->q, &hdr, buf) < 0) return -1;
    return 0;
}

int pcapio_batch_flush(PcapIO *h, PcapBatch *b) {
    if (!b->q->len) return 0;
    /* sync=0 -> kernel may coalesce; faster than the synchronized path. */
    u_int sent = pcap_sendqueue_transmit(h->p, b->q, 0);
    /* Reset by re-allocating: pcap_sendqueue has no reset API. Cheap --
     * it's a contiguous memory allocation. */
    u_int cap = b->q->maxlen;
    pcap_sendqueue_destroy(b->q);
    b->q = pcap_sendqueue_alloc(cap);
    return (b->q && sent == (u_int)-1) ? -1 : 0;
}

void pcapio_batch_free(PcapBatch *b) {
    if (!b) return;
    if (b->q) pcap_sendqueue_destroy(b->q);
    free(b);
}

void pcapio_close(PcapIO *h) {
    if (!h) return;
    if (h->p) pcap_close(h->p);
    free(h);
}
