#ifndef NISHRO_PCAP_IO_H
#define NISHRO_PCAP_IO_H

#include <stdint.h>
#include <stddef.h>

typedef struct PcapIO PcapIO;

/* Open an Npcap capture handle on the given device (e.g.
 * "\\Device\\NPF_{GUID}"). Returns NULL on error. */
PcapIO *pcapio_open(const char *device, int promiscuous, char *errbuf_256);

/* Set a BPF filter. Pass NULL to clear. */
int pcapio_set_filter(PcapIO *h, const char *filter);

/* Wait up to timeout_ms for a packet and return it in *out_buf/*out_len.
 * Returns 1 on packet, 0 on timeout, -1 on error. out_buf points into
 * Npcap internal memory -- valid only until the next call. */
int pcapio_next(PcapIO *h, int timeout_ms,
                const uint8_t **out_buf, size_t *out_len);

/* Drain all packets currently buffered by pcap (non-blocking). For each
 * packet the callback is invoked with (buf, len, userdata). Returns the
 * number of packets processed, or -1 on error. */
typedef void (*pcapio_cb)(const uint8_t *buf, size_t len, void *ud);
int pcapio_dispatch(PcapIO *h, int max_packets, pcapio_cb cb, void *ud);

int pcapio_send(PcapIO *h, const uint8_t *buf, size_t len);

/* Batched send via Npcap's pcap_sendqueue. A single PcapBatch is filled
 * with many packets then flushed in one kernel transition -- dramatically
 * faster than N calls to pcap_sendpacket for a TFTP window. */
typedef struct PcapBatch PcapBatch;
PcapBatch *pcapio_batch_new(size_t capacity_bytes);
/* Returns 0 on success, -1 if the batch is full (caller should flush). */
int  pcapio_batch_add(PcapBatch *b, const uint8_t *buf, size_t len);
/* Transmit everything queued and reset the batch for reuse. */
int  pcapio_batch_flush(PcapIO *h, PcapBatch *b);
void pcapio_batch_free(PcapBatch *b);

void pcapio_close(PcapIO *h);

#endif
