#ifndef NISHRO_SESSION_H
#define NISHRO_SESSION_H

#include <stdint.h>
#include <stddef.h>
#include "config.h"
#include "source.h"
#include "pcap_io.h"
#include "tftp.h"

typedef struct SessMgr SessMgr;

SessMgr *sessmgr_new(const Config *cfg, PcapIO *io);
void     sessmgr_free(SessMgr *m);

/* Called from main when config has been reloaded. */
void sessmgr_config_changed(SessMgr *m, const Config *cfg);

/* Feed an inbound TFTP UDP packet (just the TFTP payload + client
 * context). Returns 0 on handled, <0 on error. This dispatches to an
 * existing session or creates a new one for RRQ/WRQ. */
int sessmgr_on_tftp(SessMgr *m,
                    const uint8_t client_mac[6], uint32_t client_ip_be,
                    uint16_t client_port, uint16_t server_port,
                    uint16_t vlan_id,
                    const uint8_t *tftp, size_t tftp_len);

/* Tick all sessions: drives retransmission + timeouts. Should be
 * called at least every 100ms. */
void sessmgr_tick(SessMgr *m);

/* Counters for stats IPC. */
typedef struct {
    uint64_t bytes_sent;
    uint64_t bytes_received;
    uint32_t sessions_active;
    uint32_t sessions_total;
    uint32_t sessions_completed;
    uint32_t sessions_failed;
    uint32_t rrq_count;
    uint32_t wrq_count;
    uint32_t errors_sent;
    uint32_t acl_denied;
} SessStats;

void sessmgr_get_stats(const SessMgr *m, SessStats *out);

/* Emit a session_start or session_end line to stdout (IPC). Called
 * internally; exposed for debugging if needed. */

#endif
