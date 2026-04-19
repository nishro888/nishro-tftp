#ifndef NISHRO_JSON_H
#define NISHRO_JSON_H

#include <stdint.h>
#include <stddef.h>

/* Minimal JSON parser -- purpose-built for flat config objects and IPC
 * commands. Supports: objects, arrays, strings (no unicode escapes
 * beyond \" \\ \/ \n \r \t), numbers (integer + fraction), booleans,
 * null. All tokens are views into the source buffer; the caller owns
 * the buffer and must keep it alive while tokens are used. */

typedef enum {
    JN_NULL,
    JN_BOOL,
    JN_NUM,
    JN_STR,
    JN_ARR,
    JN_OBJ,
} JnType;

typedef struct JsonNode {
    JnType type;
    const char *src;    /* not NUL-terminated for strings */
    size_t src_len;     /* raw length; use jn_str_copy to get decoded */
    /* For arrays and objects: linked list of children via ->next.
     * For objects: child->key / child->key_len is the field name. */
    const char *key;
    size_t key_len;
    struct JsonNode *first_child;
    struct JsonNode *next;
    /* Typed cache */
    int b;
    double n;
} JsonNode;

/* Parse a JSON document. Returns a tree allocated via malloc; free with
 * json_free(). Returns NULL on parse error (err_msg filled if given). */
JsonNode *json_parse(const char *buf, size_t len, char *err_msg, size_t err_cap);

void json_free(JsonNode *root);

/* Object field lookup -- returns NULL if not present. */
const JsonNode *jn_field(const JsonNode *obj, const char *key);

/* Typed accessors -- return provided default if field missing or
 * type mismatch. */
int         jn_get_bool (const JsonNode *obj, const char *k, int def);
long long   jn_get_int  (const JsonNode *obj, const char *k, long long def);
double      jn_get_num  (const JsonNode *obj, const char *k, double def);

/* Copy string field into dst (NUL-terminated, truncated to dst_cap).
 * Returns bytes written (excluding NUL), or -1 if field absent. */
int jn_get_str(const JsonNode *obj, const char *k, char *dst, size_t dst_cap);

/* Decode a JN_STR node into a caller buffer. */
int jn_str_copy(const JsonNode *n, char *dst, size_t dst_cap);

#endif
