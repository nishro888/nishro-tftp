#ifndef NISHRO_CONFIG_H
#define NISHRO_CONFIG_H

#include <stdint.h>
#include <stddef.h>

#define MAX_ACL_RULES   64
#define MAX_PATHLEN     512

typedef struct {
    uint16_t vlan;       /* 0..4094 or 0xFFFF == "any" */
    uint32_t ip_be;      /* network order; 0 == "any" */
    uint8_t  mask_bits;  /* 0..32; 0 == "any" */
    uint8_t  allow;      /* 1 = allow, 0 = deny */
    uint8_t  svc_arp;
    uint8_t  svc_icmp;
    uint8_t  svc_tftp;
} AclRule;

typedef struct {
    /* Network */
    char     device[256];
    uint32_t virtual_ip_be;
    uint8_t  virtual_mac[6];
    uint8_t  promiscuous;

    /* TFTP */
    uint16_t tftp_port;
    uint8_t  wrq_enabled;
    uint8_t  allow_wrq_ftp;

    char     rrq_root[MAX_PATHLEN];
    char     wrq_root[MAX_PATHLEN];
    uint8_t  rrq_recursive;
    uint64_t max_rrq_size;
    uint64_t max_wrq_size;
    uint64_t max_ftp_size;

    /* Option negotiation policies -- for simplicity in C engine we
     * honor client requests within [min,max] and default to "accept
     * client value if in range, else clamp". */
    uint32_t blksize_min,    blksize_max,    blksize_default;
    uint32_t windowsize_min, windowsize_max, windowsize_default;
    uint32_t timeout_min,    timeout_max,    timeout_default;

    /* Session cap */
    uint32_t max_sessions;

    /* ACL */
    AclRule  acl[MAX_ACL_RULES];
    size_t   acl_count;
    uint8_t  acl_default_allow;

    /* FTP prefix routing */
    uint8_t  prefix_enabled;
    char     prefix_trigger[16];      /* e.g. "f::" */
    char     prefix_folder_fmt[64];   /* e.g. "BDCOM%04u" */

    /* FTP */
    uint8_t  ftp_enabled;
    char     ftp_host[256];
    uint16_t ftp_port;
    char     ftp_user[128];
    char     ftp_pass[128];
    char     ftp_root[MAX_PATHLEN];

    /* Logging */
    int      log_level;
} Config;

/* Parse a JSON object (from Python IPC) into cfg. Returns 0 on success,
 * -1 on parse error. Unknown fields are ignored. Fields not present
 * retain their previous value. */
int config_apply_json(Config *cfg, const char *json, size_t len);

/* Apply config from an already-parsed JSON object node. Used when the
 * IPC layer has parsed {"op":"config","data":{...}} and wants to feed
 * the inner "data" object directly without re-serializing. */
struct JsonNode;
int config_apply_node(Config *cfg, const struct JsonNode *root);

/* Reset config to safe defaults. */
void config_defaults(Config *cfg);

#endif
