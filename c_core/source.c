#include "source.h"
#include "config.h"
#include "ftp.h"
#include "util.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include <sys/stat.h>

/* The global config pointer -- set by main at startup. Source layer
 * only reads from it, never mutates. */
extern const Config *g_cfg;

typedef enum { SRC_LOCAL, SRC_FTP_STAGED } SrcKind;

struct FileSource {
    SrcKind kind;
    int is_write;                       /* 0 = reader, 1 = writer */
    FILE *fp;
    uint64_t size;                      /* total bytes (reader only) */

    /* for SRC_FTP_STAGED: staging file path + remote path. On commit
     * (WRQ) we upload from staging to remote. On close (RRQ) we remove
     * the staging file. */
    char staging[MAX_PATHLEN];
    char remote[MAX_PATHLEN];
    int  upload_on_commit;
};

/* Detect ftp:// prefix (case-insensitive). */
static int has_ftp_url(const char *fn) {
    return (fn[0]|32)=='f' && (fn[1]|32)=='t' && (fn[2]|32)=='p' &&
           fn[3]==':' && fn[4]=='/' && fn[5]=='/';
}

/* Rewrite f::<N>/<path> to <ftp_root>/<folder_fmt% N>/<path>.
 * Returns 0 on success, -1 on malformed. */
static int rewrite_prefix(const char *fn, char *out, size_t cap) {
    const char *p = fn + strlen(g_cfg->prefix_trigger);
    char digits[16]; size_t di = 0;
    while (*p && *p >= '0' && *p <= '9' && di + 1 < sizeof digits) digits[di++] = *p++;
    digits[di] = 0;
    if (!di) return -1;
    if (*p != '/' && *p != '\\') return -1;
    p++;
    unsigned n = (unsigned)atoi(digits);
    char folder[64];
    snprintf(folder, sizeof folder, g_cfg->prefix_folder_fmt, n);
    if (g_cfg->ftp_root[0])
        snprintf(out, cap, "%s/%s/%s", g_cfg->ftp_root, folder, p);
    else
        snprintf(out, cap, "%s/%s", folder, p);
    return 0;
}

/* Parse ftp://HOST[:PORT]/PATH -> host/port/path. Host buffer must be
 * >= 256, path must be >= MAX_PATHLEN. */
static int parse_ftp_url(const char *url, char *host, uint16_t *port, char *path) {
    const char *p = url + 6;                    /* skip ftp:// */
    const char *slash = strchr(p, '/');
    if (!slash) return -1;
    size_t hlen = (size_t)(slash - p);
    if (hlen >= 256) return -1;
    memcpy(host, p, hlen); host[hlen] = 0;
    *port = 21;
    char *colon = strchr(host, ':');
    if (colon) { *colon = 0; *port = (uint16_t)atoi(colon + 1); }
    str_copy(path, slash + 1, MAX_PATHLEN);
    return 0;
}

/* Safely join root + filename, rejecting escapes (..). Returns 0/-1. */
static int safe_join(const char *root, const char *name, char *out, size_t cap) {
    if (strstr(name, "..")) return -1;
    if (name[0] == '/' || name[0] == '\\' ||
        (name[1] == ':' && (name[2] == '/' || name[2] == '\\'))) return -1;
    snprintf(out, cap, "%s/%s", root, name);
    /* Normalize slashes for fopen on Windows. */
    for (char *q = out; *q; q++) if (*q == '/') *q = '\\';
    return 0;
}

/* Open a reader -- dispatches by filename shape. */
FileSource *src_open_read(const char *fn, uint64_t *out_size) {
    if (has_ftp_url(fn)) {
        if (!g_cfg->ftp_enabled) { LOGW("ftp:// requested but ftp disabled"); return NULL; }
        char host[256]; uint16_t port; char remote[MAX_PATHLEN];
        if (parse_ftp_url(fn, host, &port, remote) < 0) return NULL;

        char tmp[MAX_PATHLEN];
        char tmpdir[MAX_PATH];
        GetTempPathA(sizeof tmpdir, tmpdir);
        snprintf(tmp, sizeof tmp, "%s\\nishro_stage_%llu.bin",
                 tmpdir, (unsigned long long)now_ms());
        if (ftp_download(host, port,
                         g_cfg->ftp_user, g_cfg->ftp_pass,
                         remote, tmp) < 0) {
            LOGW("ftp download failed: %s", fn);
            return NULL;
        }
        FILE *fp = fopen(tmp, "rb");
        if (!fp) { remove(tmp); return NULL; }
        fseek(fp, 0, SEEK_END);
        long sz = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        FileSource *s = calloc(1, sizeof(*s));
        if (!s) { fclose(fp); remove(tmp); return NULL; }
        s->kind = SRC_FTP_STAGED;
        s->fp = fp;
        s->size = sz > 0 ? (uint64_t)sz : 0;
        str_copy(s->staging, tmp, sizeof s->staging);
        if (out_size) *out_size = s->size;
        return s;
    }

    if (g_cfg->prefix_enabled && g_cfg->prefix_trigger[0] &&
        !strncmp(fn, g_cfg->prefix_trigger, strlen(g_cfg->prefix_trigger))) {
        if (!g_cfg->ftp_enabled) return NULL;
        char remote[MAX_PATHLEN];
        if (rewrite_prefix(fn, remote, sizeof remote) < 0) return NULL;
        /* Re-enter as ftp:// form using configured host. */
        char urlbuf[MAX_PATHLEN + 64];
        snprintf(urlbuf, sizeof urlbuf, "ftp://%s:%u/%s",
                 g_cfg->ftp_host, g_cfg->ftp_port, remote);
        return src_open_read(urlbuf, out_size);
    }

    /* Local file. */
    char path[MAX_PATHLEN];
    if (safe_join(g_cfg->rrq_root, fn, path, sizeof path) < 0) return NULL;
    FILE *fp = fopen(path, "rb");
    if (!fp) return NULL;
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    FileSource *s = calloc(1, sizeof(*s));
    if (!s) { fclose(fp); return NULL; }
    s->kind = SRC_LOCAL;
    s->fp = fp;
    s->size = sz > 0 ? (uint64_t)sz : 0;
    if (out_size) *out_size = s->size;
    return s;
}

FileSource *src_open_write(const char *fn) {
    int is_ftp_name = has_ftp_url(fn) ||
        (g_cfg->prefix_enabled && g_cfg->prefix_trigger[0] &&
         !strncmp(fn, g_cfg->prefix_trigger, strlen(g_cfg->prefix_trigger)));

    if (is_ftp_name && !g_cfg->allow_wrq_ftp) {
        LOGW("wrq->ftp denied by policy: %s", fn);
        return NULL;
    }

    if (is_ftp_name) {
        /* Stage in WRQ root, upload on commit. */
        char tmpdir[MAX_PATH];
        GetTempPathA(sizeof tmpdir, tmpdir);
        char tmp[MAX_PATHLEN];
        snprintf(tmp, sizeof tmp, "%s\\nishro_wrq_%llu.bin",
                 tmpdir, (unsigned long long)now_ms());
        FILE *fp = fopen(tmp, "wb");
        if (!fp) return NULL;
        FileSource *s = calloc(1, sizeof(*s));
        if (!s) { fclose(fp); remove(tmp); return NULL; }
        s->kind = SRC_FTP_STAGED;
        s->is_write = 1;
        s->fp = fp;
        s->upload_on_commit = 1;
        str_copy(s->staging, tmp, sizeof s->staging);
        if (has_ftp_url(fn)) {
            str_copy(s->remote, fn, sizeof s->remote);
        } else {
            char r[MAX_PATHLEN];
            rewrite_prefix(fn, r, sizeof r);
            snprintf(s->remote, sizeof s->remote, "ftp://%s:%u/%s",
                     g_cfg->ftp_host, g_cfg->ftp_port, r);
        }
        return s;
    }

    /* Local write -- into wrq_root. */
    char path[MAX_PATHLEN];
    if (safe_join(g_cfg->wrq_root, fn, path, sizeof path) < 0) return NULL;
    FILE *fp = fopen(path, "wb");
    if (!fp) return NULL;
    FileSource *s = calloc(1, sizeof(*s));
    if (!s) { fclose(fp); return NULL; }
    s->kind = SRC_LOCAL;
    s->is_write = 1;
    s->fp = fp;
    return s;
}

int src_read(FileSource *s, void *buf, size_t max) {
    if (!s || s->is_write || !s->fp) return -1;
    size_t r = fread(buf, 1, max, s->fp);
    if (r == 0 && ferror(s->fp)) return -1;
    return (int)r;
}

int src_seek(FileSource *s, uint64_t off) {
    if (!s || !s->fp) return -1;
    return fseek(s->fp, (long)off, SEEK_SET);
}

int src_write(FileSource *s, const void *buf, size_t len) {
    if (!s || !s->is_write || !s->fp) return -1;
    return fwrite(buf, 1, len, s->fp) == len ? 0 : -1;
}

int src_commit_write(FileSource *s) {
    if (!s || !s->is_write) { src_close(s); return -1; }
    if (s->fp) { fclose(s->fp); s->fp = NULL; }
    int rv = 0;
    if (s->kind == SRC_FTP_STAGED && s->upload_on_commit) {
        char host[256]; uint16_t port; char remote[MAX_PATHLEN];
        if (parse_ftp_url(s->remote, host, &port, remote) < 0) rv = -1;
        else rv = ftp_upload(host, port,
                             g_cfg->ftp_user, g_cfg->ftp_pass,
                             s->staging, remote);
        if (s->staging[0]) remove(s->staging);
    }
    free(s);
    return rv;
}

void src_close(FileSource *s) {
    if (!s) return;
    if (s->fp) fclose(s->fp);
    /* Remove staging on reader-close or aborted write. */
    if (s->kind == SRC_FTP_STAGED && s->staging[0]) remove(s->staging);
    free(s);
}
