#include "json.h"
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

typedef struct {
    const char *p, *end;
    char *err;
    size_t err_cap;
} Parser;

static void set_err(Parser *pp, const char *msg) {
    if (!pp->err || !pp->err_cap) return;
    size_t n = strlen(msg);
    if (n >= pp->err_cap) n = pp->err_cap - 1;
    memcpy(pp->err, msg, n);
    pp->err[n] = 0;
}

static void skip_ws(Parser *pp) {
    while (pp->p < pp->end) {
        char c = *pp->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pp->p++;
        else break;
    }
}

static JsonNode *mknode(JnType t) {
    JsonNode *n = calloc(1, sizeof(*n));
    if (n) n->type = t;
    return n;
}

static JsonNode *parse_value(Parser *pp);

static int parse_string_raw(Parser *pp, const char **out, size_t *out_len) {
    if (pp->p >= pp->end || *pp->p != '"') { set_err(pp, "expected string"); return -1; }
    pp->p++;
    const char *s = pp->p;
    while (pp->p < pp->end && *pp->p != '"') {
        if (*pp->p == '\\') {
            pp->p++;
            if (pp->p >= pp->end) { set_err(pp, "bad escape"); return -1; }
        }
        pp->p++;
    }
    if (pp->p >= pp->end) { set_err(pp, "unterminated string"); return -1; }
    *out = s;
    *out_len = (size_t)(pp->p - s);
    pp->p++;
    return 0;
}

static JsonNode *parse_number(Parser *pp) {
    const char *s = pp->p;
    if (*pp->p == '-' || *pp->p == '+') pp->p++;
    while (pp->p < pp->end && (isdigit((unsigned char)*pp->p) || *pp->p == '.' ||
                               *pp->p == 'e' || *pp->p == 'E' || *pp->p == '+' || *pp->p == '-'))
        pp->p++;
    JsonNode *n = mknode(JN_NUM);
    if (!n) return NULL;
    n->src = s;
    n->src_len = (size_t)(pp->p - s);
    char tmp[64];
    size_t l = n->src_len < sizeof(tmp) - 1 ? n->src_len : sizeof(tmp) - 1;
    memcpy(tmp, s, l); tmp[l] = 0;
    n->n = strtod(tmp, NULL);
    return n;
}

static JsonNode *parse_array(Parser *pp) {
    pp->p++;                 /* skip [ */
    JsonNode *arr = mknode(JN_ARR);
    if (!arr) return NULL;
    JsonNode **tail = &arr->first_child;
    skip_ws(pp);
    if (pp->p < pp->end && *pp->p == ']') { pp->p++; return arr; }
    for (;;) {
        skip_ws(pp);
        JsonNode *v = parse_value(pp);
        if (!v) { json_free(arr); return NULL; }
        *tail = v; tail = &v->next;
        skip_ws(pp);
        if (pp->p >= pp->end) { set_err(pp, "array eof"); json_free(arr); return NULL; }
        if (*pp->p == ',') { pp->p++; continue; }
        if (*pp->p == ']') { pp->p++; return arr; }
        set_err(pp, "expected , or ]"); json_free(arr); return NULL;
    }
}

static JsonNode *parse_object(Parser *pp) {
    pp->p++;                 /* skip { */
    JsonNode *obj = mknode(JN_OBJ);
    if (!obj) return NULL;
    JsonNode **tail = &obj->first_child;
    skip_ws(pp);
    if (pp->p < pp->end && *pp->p == '}') { pp->p++; return obj; }
    for (;;) {
        skip_ws(pp);
        const char *k; size_t klen;
        if (parse_string_raw(pp, &k, &klen) < 0) { json_free(obj); return NULL; }
        skip_ws(pp);
        if (pp->p >= pp->end || *pp->p != ':') {
            set_err(pp, "expected :"); json_free(obj); return NULL;
        }
        pp->p++;
        skip_ws(pp);
        JsonNode *v = parse_value(pp);
        if (!v) { json_free(obj); return NULL; }
        v->key = k; v->key_len = klen;
        *tail = v; tail = &v->next;
        skip_ws(pp);
        if (pp->p >= pp->end) { set_err(pp, "object eof"); json_free(obj); return NULL; }
        if (*pp->p == ',') { pp->p++; continue; }
        if (*pp->p == '}') { pp->p++; return obj; }
        set_err(pp, "expected , or }"); json_free(obj); return NULL;
    }
}

static JsonNode *parse_value(Parser *pp) {
    skip_ws(pp);
    if (pp->p >= pp->end) { set_err(pp, "unexpected eof"); return NULL; }
    char c = *pp->p;
    if (c == '{') return parse_object(pp);
    if (c == '[') return parse_array(pp);
    if (c == '"') {
        JsonNode *n = mknode(JN_STR);
        if (!n) return NULL;
        if (parse_string_raw(pp, &n->src, &n->src_len) < 0) { free(n); return NULL; }
        return n;
    }
    if (c == 't' && pp->end - pp->p >= 4 && !memcmp(pp->p, "true", 4)) {
        pp->p += 4; JsonNode *n = mknode(JN_BOOL); if (n) n->b = 1; return n;
    }
    if (c == 'f' && pp->end - pp->p >= 5 && !memcmp(pp->p, "false", 5)) {
        pp->p += 5; JsonNode *n = mknode(JN_BOOL); if (n) n->b = 0; return n;
    }
    if (c == 'n' && pp->end - pp->p >= 4 && !memcmp(pp->p, "null", 4)) {
        pp->p += 4; return mknode(JN_NULL);
    }
    if (c == '-' || c == '+' || (c >= '0' && c <= '9')) return parse_number(pp);
    set_err(pp, "bad value");
    return NULL;
}

JsonNode *json_parse(const char *buf, size_t len, char *err, size_t err_cap) {
    Parser pp = { .p = buf, .end = buf + len, .err = err, .err_cap = err_cap };
    JsonNode *r = parse_value(&pp);
    if (!r) return NULL;
    skip_ws(&pp);
    /* trailing bytes are ok -- caller may batch messages */
    return r;
}

void json_free(JsonNode *root) {
    if (!root) return;
    JsonNode *c = root->first_child;
    while (c) { JsonNode *nx = c->next; json_free(c); c = nx; }
    free(root);
}

const JsonNode *jn_field(const JsonNode *obj, const char *key) {
    if (!obj || obj->type != JN_OBJ) return NULL;
    size_t kl = strlen(key);
    for (JsonNode *c = obj->first_child; c; c = c->next) {
        if (c->key_len == kl && !memcmp(c->key, key, kl)) return c;
    }
    return NULL;
}

int jn_str_copy(const JsonNode *n, char *dst, size_t cap) {
    if (!n || n->type != JN_STR || !cap) return -1;
    size_t o = 0;
    for (size_t i = 0; i < n->src_len && o + 1 < cap; i++) {
        char c = n->src[i];
        if (c == '\\' && i + 1 < n->src_len) {
            char e = n->src[++i];
            switch (e) {
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case '"': case '\\': case '/': c = e; break;
            default: c = e; break;   /* ignore unicode escapes */
            }
        }
        dst[o++] = c;
    }
    dst[o] = 0;
    return (int)o;
}

int jn_get_bool(const JsonNode *obj, const char *k, int def) {
    const JsonNode *f = jn_field(obj, k);
    if (!f) return def;
    if (f->type == JN_BOOL) return f->b;
    if (f->type == JN_NUM)  return f->n != 0;
    return def;
}

long long jn_get_int(const JsonNode *obj, const char *k, long long def) {
    const JsonNode *f = jn_field(obj, k);
    if (!f || f->type != JN_NUM) return def;
    return (long long)f->n;
}

double jn_get_num(const JsonNode *obj, const char *k, double def) {
    const JsonNode *f = jn_field(obj, k);
    if (!f || f->type != JN_NUM) return def;
    return f->n;
}

int jn_get_str(const JsonNode *obj, const char *k, char *dst, size_t cap) {
    const JsonNode *f = jn_field(obj, k);
    if (!f || f->type != JN_STR) { if (cap) dst[0] = 0; return -1; }
    return jn_str_copy(f, dst, cap);
}
