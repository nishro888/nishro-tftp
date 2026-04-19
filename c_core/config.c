#include "config.h"
#include "json.h"
#include "util.h"
#include <string.h>
#include <stdlib.h>

void config_defaults(Config *c) {
    memset(c, 0, sizeof(*c));
    c->promiscuous = 1;
    c->tftp_port = 69;
    c->wrq_enabled = 0;
    c->allow_wrq_ftp = 0;
    str_copy(c->rrq_root, ".\\tftp_root", sizeof(c->rrq_root));
    str_copy(c->wrq_root, ".\\tftp_uploads", sizeof(c->wrq_root));
    c->rrq_recursive = 0;
    c->max_rrq_size = 0;
    c->max_wrq_size = 0;
    c->max_ftp_size = 0;

    c->blksize_min = 8;     c->blksize_max    = 65464; c->blksize_default    = 1468;
    /* Bigger default windowsize = fewer round-trips between data bursts.
     * 64 in-flight blocks at 1468 bytes = ~94 KB per ACK cycle. */
    c->windowsize_min = 1;  c->windowsize_max = 64;    c->windowsize_default = 32;
    c->timeout_min = 1;     c->timeout_max    = 255;   c->timeout_default    = 5;

    c->max_sessions = 64;
    c->acl_default_allow = 1;

    c->prefix_enabled = 0;
    str_copy(c->prefix_trigger, "f::", sizeof(c->prefix_trigger));
    str_copy(c->prefix_folder_fmt, "BDCOM%04u", sizeof(c->prefix_folder_fmt));

    c->ftp_enabled = 0;
    c->ftp_port = 21;

    c->log_level = LOG_INFO;
}

static uint16_t get_vlan_field(const JsonNode *r) {
    const JsonNode *f = jn_field(r, "vlan");
    if (!f) return 0xFFFF;
    if (f->type == JN_NULL) return 0xFFFF;
    if (f->type == JN_STR) {
        char buf[16];
        jn_str_copy(f, buf, sizeof buf);
        if (!buf[0] || !strcmp(buf, "any") || !strcmp(buf, "*")) return 0xFFFF;
        return (uint16_t)atoi(buf);
    }
    if (f->type == JN_NUM) return (uint16_t)f->n;
    return 0xFFFF;
}

static void apply_acl(Config *c, const JsonNode *arr) {
    c->acl_count = 0;
    if (!arr || arr->type != JN_ARR) return;
    for (JsonNode *r = arr->first_child; r; r = r->next) {
        if (c->acl_count >= MAX_ACL_RULES) break;
        if (r->type != JN_OBJ) continue;
        AclRule *rr = &c->acl[c->acl_count];
        memset(rr, 0, sizeof(*rr));

        rr->vlan = get_vlan_field(r);

        char ipbuf[64] = {0};
        jn_get_str(r, "ip", ipbuf, sizeof ipbuf);
        if (!ipbuf[0]) {
            rr->ip_be = 0; rr->mask_bits = 0;
        } else {
            char *slash = strchr(ipbuf, '/');
            int mask = 32;
            if (slash) { *slash = 0; mask = atoi(slash + 1); }
            if (parse_ipv4(ipbuf, &rr->ip_be) == 0) rr->mask_bits = (uint8_t)mask;
            else { rr->ip_be = 0; rr->mask_bits = 0; }
        }

        rr->allow = (uint8_t)jn_get_bool(r, "allow", 1);
        const JsonNode *sv = jn_field(r, "services");
        if (sv && sv->type == JN_OBJ) {
            rr->svc_arp  = (uint8_t)jn_get_bool(sv, "arp",  1);
            rr->svc_icmp = (uint8_t)jn_get_bool(sv, "icmp", 1);
            rr->svc_tftp = (uint8_t)jn_get_bool(sv, "tftp", 1);
        } else {
            rr->svc_arp = rr->svc_icmp = rr->svc_tftp = 1;
        }
        c->acl_count++;
    }
}

int config_apply_node(Config *c, const JsonNode *root) {
    if (!root) return -1;
    char tmp[MAX_PATHLEN];
    char macbuf[32];

    const JsonNode *net = jn_field(root, "network");
    if (net && net->type == JN_OBJ) {
        jn_get_str(net, "nic", c->device, sizeof c->device);
        if (jn_get_str(net, "virtual_ip", tmp, sizeof tmp) >= 0 && tmp[0])
            parse_ipv4(tmp, &c->virtual_ip_be);
        if (jn_get_str(net, "virtual_mac", macbuf, sizeof macbuf) >= 0 && macbuf[0])
            parse_mac(macbuf, c->virtual_mac);
        c->promiscuous = (uint8_t)jn_get_bool(net, "promiscuous", 1);
    }

    const JsonNode *tftp = jn_field(root, "tftp");
    if (tftp && tftp->type == JN_OBJ) {
        c->tftp_port = (uint16_t)jn_get_int(tftp, "port", 69);
        c->wrq_enabled = (uint8_t)jn_get_bool(tftp, "wrq_enabled", 0);
        const JsonNode *opts = jn_field(tftp, "options");
        if (opts && opts->type == JN_OBJ) {
            const JsonNode *bs = jn_field(opts, "blksize");
            if (bs && bs->type == JN_OBJ) {
                c->blksize_min     = (uint32_t)jn_get_int(bs, "min",     c->blksize_min);
                c->blksize_max     = (uint32_t)jn_get_int(bs, "max",     c->blksize_max);
                c->blksize_default = (uint32_t)jn_get_int(bs, "default", c->blksize_default);
            }
            const JsonNode *ws = jn_field(opts, "windowsize");
            if (ws && ws->type == JN_OBJ) {
                c->windowsize_min     = (uint32_t)jn_get_int(ws, "min",     c->windowsize_min);
                c->windowsize_max     = (uint32_t)jn_get_int(ws, "max",     c->windowsize_max);
                c->windowsize_default = (uint32_t)jn_get_int(ws, "default", c->windowsize_default);
            }
            const JsonNode *to = jn_field(opts, "timeout");
            if (to && to->type == JN_OBJ) {
                c->timeout_min     = (uint32_t)jn_get_int(to, "min",     c->timeout_min);
                c->timeout_max     = (uint32_t)jn_get_int(to, "max",     c->timeout_max);
                c->timeout_default = (uint32_t)jn_get_int(to, "default", c->timeout_default);
            }
        }
    }

    const JsonNode *files = jn_field(root, "files");
    if (files && files->type == JN_OBJ) {
        jn_get_str(files, "rrq_root", c->rrq_root, sizeof c->rrq_root);
        jn_get_str(files, "wrq_root", c->wrq_root, sizeof c->wrq_root);
        c->rrq_recursive = (uint8_t)jn_get_bool(files, "rrq_recursive", 0);
        c->max_rrq_size  = (uint64_t)jn_get_int (files, "max_rrq_size", 0);
        c->max_wrq_size  = (uint64_t)jn_get_int (files, "max_wrq_size", 0);
        c->max_ftp_size  = (uint64_t)jn_get_int (files, "max_ftp_size", 0);
        c->allow_wrq_ftp = (uint8_t)jn_get_bool(files, "allow_wrq_ftp", 0);

        const JsonNode *pref = jn_field(files, "prefix");
        if (pref && pref->type == JN_OBJ) {
            c->prefix_enabled = (uint8_t)jn_get_bool(pref, "enabled", 0);
            jn_get_str(pref, "trigger",    c->prefix_trigger,    sizeof c->prefix_trigger);
            jn_get_str(pref, "folder_fmt", c->prefix_folder_fmt, sizeof c->prefix_folder_fmt);
        }

        const JsonNode *ftp = jn_field(files, "ftp");
        if (ftp && ftp->type == JN_OBJ) {
            c->ftp_enabled = (uint8_t)jn_get_bool(ftp, "enabled", 0);
            jn_get_str(ftp, "host", c->ftp_host, sizeof c->ftp_host);
            c->ftp_port = (uint16_t)jn_get_int(ftp, "port", 21);
            jn_get_str(ftp, "user", c->ftp_user, sizeof c->ftp_user);
            jn_get_str(ftp, "pass", c->ftp_pass, sizeof c->ftp_pass);
            jn_get_str(ftp, "root", c->ftp_root, sizeof c->ftp_root);
        }
    }

    const JsonNode *sess = jn_field(root, "sessions");
    if (sess && sess->type == JN_OBJ) {
        c->max_sessions = (uint32_t)jn_get_int(sess, "max", 64);
    }

    const JsonNode *sec = jn_field(root, "security");
    if (sec && sec->type == JN_OBJ) {
        c->acl_default_allow = (uint8_t)jn_get_bool(sec, "default_allow", 1);
        apply_acl(c, jn_field(sec, "rules"));
    }

    const JsonNode *log = jn_field(root, "logging");
    if (log && log->type == JN_OBJ) {
        char lv[16] = {0};
        jn_get_str(log, "level", lv, sizeof lv);
        if      (!strcmp(lv, "debug")) c->log_level = LOG_DEBUG;
        else if (!strcmp(lv, "info"))  c->log_level = LOG_INFO;
        else if (!strcmp(lv, "warn"))  c->log_level = LOG_WARN;
        else if (!strcmp(lv, "error")) c->log_level = LOG_ERROR;
    }
    g_log_level = c->log_level;
    return 0;
}

int config_apply_json(Config *c, const char *json, size_t len) {
    char err[128];
    JsonNode *root = json_parse(json, len, err, sizeof err);
    if (!root) { LOGE("config json parse: %s", err); return -1; }
    int r = config_apply_node(c, root);
    json_free(root);
    return r;
}
