/* nishro_core -- C TFTP engine for Nishro TFTP.
 *
 * Spawned as a child process by the Python management layer. Config
 * arrives as JSON on stdin (one message per line). Stats, session
 * events, and log lines are emitted as JSON on stdout.
 *
 * Command line:
 *    nishro_core --device "\\Device\\NPF_{GUID}"
 *
 * The --device flag is only needed for standalone testing; in normal
 * operation the parent sends a {"op":"config", ...} line with every
 * field including network.nic, and we open pcap from there.
 */
#include "util.h"
#include "config.h"
#include "pcap_io.h"
#include "packet.h"
#include "session.h"
#include "tftp.h"
#include "ipc.h"
#include "json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <winsock2.h>
#include <windows.h>

/* Global config -- source.c reads it via extern. */
Config g_cfg_storage;
const Config *g_cfg = &g_cfg_storage;

/* Forward decls for ARP/ICMP handlers. */
int arp_handle (PcapIO *io, const Config *cfg, const PktView *p);
int icmp_handle(PcapIO *io, const Config *cfg, const PktView *p);

static volatile int g_should_exit = 0;
static BOOL WINAPI ctrl_handler(DWORD t) { (void)t; g_should_exit = 1; return TRUE; }

typedef struct {
    PcapIO *io;
    SessMgr *sess;
    int pcap_ready;
    uint64_t last_stat_ms;
    /* Protocol counters not owned by SessMgr. */
    uint64_t arp_requests, arp_replies;
    uint64_t icmp_requests, icmp_replies;
} Runtime;

static void open_pcap_if_needed(Runtime *rt) {
    if (rt->pcap_ready) return;
    if (!g_cfg->device[0]) return;
    char err[256];
    rt->io = pcapio_open(g_cfg->device, g_cfg->promiscuous, err);
    if (!rt->io) {
        LOGE("pcap open '%s' failed: %s", g_cfg->device, err);
        return;
    }
    /* BPF filter: anything to our virtual IP plus broadcast ARP. Keeps
     * userspace packet rate down. */
    char filter[512];
    char ip[16]; ipv4_to_str(g_cfg->virtual_ip_be, ip);
    snprintf(filter, sizeof filter,
        "(arp) or (ip host %s) or (vlan and (arp or ip host %s))", ip, ip);
    pcapio_set_filter(rt->io, filter);

    rt->sess = sessmgr_new(g_cfg, rt->io);
    rt->pcap_ready = 1;
    char ready[256];
    snprintf(ready, sizeof ready,
        "{\"ev\":\"ready\",\"device\":\"%s\",\"virtual_ip\":\"%s\"}",
        g_cfg->device, ip);
    ipc_emit(ready);
    LOGI("pcap opened on %s (vip %s)", g_cfg->device, ip);
}

static void close_pcap(Runtime *rt) {
    if (rt->sess) { sessmgr_free(rt->sess); rt->sess = NULL; }
    if (rt->io)   { pcapio_close(rt->io);   rt->io = NULL; }
    rt->pcap_ready = 0;
}

static void on_pkt(Runtime *rt, const uint8_t *pkt, size_t len);

/* pcap dispatch trampoline -- handles one packet. */
static void on_pkt_cb(const uint8_t *buf, size_t len, void *ud) {
    on_pkt((Runtime *)ud, buf, len);
}

/* Handle an inbound pcap packet. */
static void on_pkt(Runtime *rt, const uint8_t *pkt, size_t len) {
    PktView v;
    if (pkt_parse(pkt, len, &v) < 0) return;

    /* Ignore our own sent frames. */
    if (!memcmp(v.src_mac, g_cfg->virtual_mac, 6)) return;

    /* Only count ARP/ICMP addressed to our virtual IP -- the NIC may be
     * in promiscuous mode and see traffic for the whole LAN. */
    if (v.is_arp && v.arp_oper == 1 && v.arp_tpa_be == g_cfg->virtual_ip_be)
        rt->arp_requests++;
    if (v.is_icmp && v.icmp_type == 8 && v.dst_ip_be == g_cfg->virtual_ip_be)
        rt->icmp_requests++;
    if (arp_handle(rt->io, g_cfg, &v))  { rt->arp_replies++;  return; }
    if (icmp_handle(rt->io, g_cfg, &v)) { rt->icmp_replies++; return; }

    if (v.is_udp && v.dst_ip_be == g_cfg->virtual_ip_be &&
        v.dst_port == g_cfg->tftp_port) {
        sessmgr_on_tftp(rt->sess,
                        v.src_mac, v.src_ip_be,
                        v.src_port, v.dst_port,
                        v.has_vlan ? v.vlan_id : 0xFFFF,
                        v.udp_payload, v.udp_payload_len);
        return;
    }
    /* Traffic addressed to an existing session's server TID. */
    if (v.is_udp && v.dst_ip_be == g_cfg->virtual_ip_be) {
        sessmgr_on_tftp(rt->sess,
                        v.src_mac, v.src_ip_be,
                        v.src_port, v.dst_port,
                        v.has_vlan ? v.vlan_id : 0xFFFF,
                        v.udp_payload, v.udp_payload_len);
    }
}

/* IPC callback: parse a control message from Python. */
static void on_ipc_line(const char *line, size_t len, void *ud) {
    Runtime *rt = ud;
    char err[128];
    JsonNode *root = json_parse(line, len, err, sizeof err);
    if (!root) { LOGW("ipc bad json: %s", err); return; }

    char op[32] = {0};
    jn_get_str(root, "op", op, sizeof op);

    if (!strcmp(op, "config")) {
        const JsonNode *data = jn_field(root, "data");
        if (data && data->type == JN_OBJ) {
            config_apply_node(&g_cfg_storage, data);
            if (rt->pcap_ready) {
                /* Config changes within same NIC/VIP -- just update sess. */
                sessmgr_config_changed(rt->sess, g_cfg);
            } else {
                open_pcap_if_needed(rt);
            }
        }
    } else if (!strcmp(op, "stop")) {
        g_should_exit = 1;
    } else if (!strcmp(op, "ping")) {
        ipc_emit("{\"ev\":\"pong\"}");
    }
    json_free(root);
}

static void emit_stats(Runtime *rt) {
    if (!rt->sess) return;
    SessStats s;
    sessmgr_get_stats(rt->sess, &s);
    char buf[768];
    snprintf(buf, sizeof buf,
        "{\"ev\":\"stat\",\"bytes_sent\":%llu,\"bytes_received\":%llu,"
        "\"sessions_active\":%u,\"sessions_total\":%u,"
        "\"sessions_completed\":%u,\"sessions_failed\":%u,"
        "\"rrq\":%u,\"wrq\":%u,\"errors\":%u,\"acl_denied\":%u,"
        "\"arp_requests\":%llu,\"arp_replies\":%llu,"
        "\"icmp_requests\":%llu,\"icmp_replies\":%llu}",
        (unsigned long long)s.bytes_sent,
        (unsigned long long)s.bytes_received,
        s.sessions_active, s.sessions_total,
        s.sessions_completed, s.sessions_failed,
        s.rrq_count, s.wrq_count, s.errors_sent, s.acl_denied,
        (unsigned long long)rt->arp_requests,
        (unsigned long long)rt->arp_replies,
        (unsigned long long)rt->icmp_requests,
        (unsigned long long)rt->icmp_replies);
    ipc_emit(buf);
}

/* Npcap installs wpcap.dll under %SystemRoot%\System32\Npcap, which isn't
 * on the default DLL search path. Add it before any pcap call. */
static void add_npcap_dll_dir(void) {
    char p[MAX_PATH];
    UINT n = GetSystemDirectoryA(p, sizeof p);
    if (!n || n >= sizeof p - 8) return;
    strcpy(p + n, "\\Npcap");
    SetDllDirectoryA(p);
}

int main(int argc, char **argv) {
    /* Fully buffer stdout and flush ourselves in the main loop. The
     * parent reads newline-delimited JSON and each line is flushed by
     * ipc_emit (see ipc.c). Fully-buffered stdio avoids an implicit
     * per-fputc flush under _IOLBF on the Windows pipe, which matters
     * when session_progress fires inside the DATA send loop. */
    setvbuf(stdout, NULL, _IOFBF, 16384);
    setvbuf(stderr, NULL, _IOLBF, 4096);
    SetConsoleCtrlHandler(ctrl_handler, TRUE);
    add_npcap_dll_dir();

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        LOGE("WSAStartup failed");
        return 1;
    }

    config_defaults(&g_cfg_storage);
    g_log_level = g_cfg_storage.log_level;

    /* Accept optional --device as a smoke-test convenience. */
    for (int i = 1; i < argc - 1; i++) {
        if (!strcmp(argv[i], "--device")) {
            str_copy(g_cfg_storage.device, argv[i + 1], sizeof g_cfg_storage.device);
        } else if (!strcmp(argv[i], "--log-level")) {
            const char *lv = argv[i + 1];
            if      (!strcmp(lv, "debug")) g_log_level = LOG_DEBUG;
            else if (!strcmp(lv, "warn"))  g_log_level = LOG_WARN;
            else if (!strcmp(lv, "error")) g_log_level = LOG_ERROR;
        }
    }

    LOGI("nishro_core starting (build %s %s)", __DATE__, __TIME__);
    ipc_emit("{\"ev\":\"hello\",\"version\":\"c-1.0\"}");

    Runtime rt = {0};

    /* If --device was passed, try to open immediately. Otherwise wait
     * for the parent to push a config message. */
    if (g_cfg_storage.device[0]) open_pcap_if_needed(&rt);

    while (!g_should_exit) {
        int ipc_r = ipc_poll(on_ipc_line, &rt);
        if (ipc_r < 0) {
            LOGI("stdin closed -- parent exited, shutting down");
            break;
        }

        if (rt.pcap_ready) {
            /* Drain everything the kernel has buffered. pcap_dispatch
             * returns immediately once the ring is empty thanks to the
             * 1ms read timeout we set in pcap_io. */
            int got = pcapio_dispatch(rt.io, 256, on_pkt_cb, &rt);
            if (got < 0) break;
            sessmgr_tick(rt.sess);
            /* When idle, pcap blocks for up to 1ms inside pcap_dispatch;
             * that's our loop cadence under no load. When busy, the
             * dispatch returns as soon as the ring is empty so there's
             * no throttling on the send side. */
        } else {
            Sleep(50);
        }

        uint64_t tnow = now_ms();
        if (tnow - rt.last_stat_ms >= 1000) {
            emit_stats(&rt);
            rt.last_stat_ms = tnow;
        }
    }

    LOGI("shutdown");
    close_pcap(&rt);
    ipc_emit("{\"ev\":\"bye\"}");
    WSACleanup();
    return 0;
}
