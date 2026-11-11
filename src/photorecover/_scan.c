/* photorecover._scan: a resumable baseline-JPEG entropy decoder.
 *
 * It decodes Huffman-coded scan data one MCU at a time and returns only the DC
 * value of every block, which is an exact 1/8-scale picture. Its state can be
 * saved at any MCU boundary and resumed with more data, so testing "does this
 * cluster continue this photo?" costs one cluster of decoding, not the whole
 * photo. It also reports the first place the stream stops being valid JPEG.
 *
 * The pure-Python module _scan_py.py implements exactly the same function and
 * is used when this extension is not built. Keep the two in step.
 *
 * GPL-3.0-or-later.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

#define ST_NEED_DATA 0   /* ran out of data at an MCU boundary; resume with more */
#define ST_DONE 1        /* every MCU of the picture decoded */
#define ST_ERROR 2       /* the stream is not valid from MCU `mcu` on */

#define E_NONE 0
#define E_HUFF 1         /* no such Huffman code */
#define E_OVERRUN 2      /* more than 64 coefficients in a block */
#define E_SIZE 3         /* coefficient size too large for baseline */
#define E_MARKER 4       /* a marker in the middle of an MCU, or an unexpected one */
#define E_RST 5          /* restart marker missing or out of order */
#define E_DCRANGE 6      /* DC value outside what 8-bit samples can produce */

typedef struct {
    int32_t mincode[17], maxcode[18], valptr[17];
    uint8_t vals[256];
    int present;
} HT;

typedef struct {
    int64_t pos;         /* next unread byte */
    int32_t cur, left;   /* current byte and how many of its bits are unread */
    int32_t pred[4];
    int64_t mcu;
    int32_t to_restart, next_rst;
} State;

typedef struct {
    const uint8_t *d;
    int64_t end;
    State s;
    int eod, marker;
} Reader;

static void build_table(HT *t, const uint8_t *raw) {
    int code = 0, k = 0, l;
    t->present = 0;
    for (l = 1; l <= 16; l++) {
        int n = raw[l - 1];
        t->valptr[l] = k;
        t->mincode[l] = code;
        if (n) t->present = 1;
        code += n;
        k += n;
        t->maxcode[l] = n ? code - 1 : -1;
        code <<= 1;
    }
    t->maxcode[17] = 0x7fffffff;
    memcpy(t->vals, raw + 16, 256);
}

/* Returns 0/1, or -1 with r->eod or r->marker set. */
static inline int get_bit(Reader *r) {
    if (r->s.left == 0) {
        int64_t p = r->s.pos;
        int b;
        if (p >= r->end) { r->eod = 1; return -1; }
        b = r->d[p];
        if (b == 0xFF) {
            int n;
            if (p + 1 >= r->end) { r->eod = 1; return -1; }
            n = r->d[p + 1];
            if (n != 0) { r->marker = n; return -1; }
            r->s.pos = p + 2;
        } else {
            r->s.pos = p + 1;
        }
        r->s.cur = b;
        r->s.left = 8;
    }
    r->s.left--;
    return (r->s.cur >> r->s.left) & 1;
}

static inline int get_bits(Reader *r, int n) {
    int v = 0;
    while (n-- > 0) {
        int b = get_bit(r);
        if (b < 0) return -1;
        v = (v << 1) | b;
    }
    return v;
}

/* Returns the symbol, -1 on eod/marker, -2 on an invalid code. */
static inline int huff(Reader *r, const HT *t) {
    int code = 0, l;
    for (l = 1; l <= 16; l++) {
        int b = get_bit(r);
        if (b < 0) return -1;
        code = (code << 1) | b;
        if (t->maxcode[l] >= 0 && code <= t->maxcode[l] && code >= t->mincode[l])
            return t->vals[t->valptr[l] + code - t->mincode[l]];
    }
    return -2;
}

typedef struct {
    int32_t ncomp, nblocks[4], dctab[4], actab[4], dcbound[4], restart;
    int64_t total;
    int blocks_per_mcu;
    HT tabs[8];
} Spec;

#define SPEC_LEN (4 + 4 * 16 + 4 + 8 + 8 * 272)

static int parse_spec(Spec *S, const uint8_t *sp, Py_ssize_t len) {
    int i;
    if (len != SPEC_LEN) { PyErr_SetString(PyExc_ValueError, "bad spec size"); return -1; }
    memcpy(&S->ncomp, sp, 4); sp += 4;
    if (S->ncomp < 1 || S->ncomp > 4) { PyErr_SetString(PyExc_ValueError, "bad component count"); return -1; }
    S->blocks_per_mcu = 0;
    for (i = 0; i < 4; i++) {
        memcpy(&S->nblocks[i], sp, 4); memcpy(&S->dctab[i], sp + 4, 4);
        memcpy(&S->actab[i], sp + 8, 4); memcpy(&S->dcbound[i], sp + 12, 4);
        sp += 16;
        if (i < S->ncomp) {
            if (S->nblocks[i] < 1 || S->nblocks[i] > 16 || S->dctab[i] < 0 || S->dctab[i] > 3 ||
                S->actab[i] < 0 || S->actab[i] > 3) {
                PyErr_SetString(PyExc_ValueError, "bad component spec"); return -1;
            }
            S->blocks_per_mcu += S->nblocks[i];
        }
    }
    memcpy(&S->restart, sp, 4); sp += 4;
    memcpy(&S->total, sp, 8); sp += 8;
    for (i = 0; i < 8; i++) build_table(&S->tabs[i], sp + i * 272);
    return 0;
}

/* How many MCUs the data in r could possibly hold (one bit per block at least). */
static int64_t mcu_cap(const Reader *r, const Spec *S, long long max_mcus) {
    int64_t todo = S->total - r->s.mcu;
    int64_t cap = ((r->end - r->s.pos) * 8 + 8) / S->blocks_per_mcu + 1;
    if (max_mcus >= 0 && max_mcus < todo) todo = max_mcus;
    if (todo < 0) todo = 0;
    return cap < todo ? cap : todo;
}

/* Decode up to todo MCUs. On return r->s is the state at the last good MCU
 * boundary (for an error: the boundary before the bad MCU). */
static int64_t run(Reader *r, const Spec *S, int16_t *out, int64_t todo, int *status_out, int *err_out) {
    int status = ST_NEED_DATA, err = E_NONE;
    int64_t done = 0;
    State good = r->s;
    const int restart = S->restart;
    while (1) {
        good = r->s;
        if (r->s.mcu >= S->total) { status = ST_DONE; break; }
        if (done >= todo) { status = ST_NEED_DATA; break; }
        if (restart > 0 && r->s.to_restart == 0) {
            /* a restart marker is due: drop padding bits, then expect FF Dn */
            int64_t p = r->s.pos;
            while (p + 1 < r->end && r->d[p] == 0xFF && r->d[p + 1] == 0xFF) p++;
            if (p + 1 >= r->end) { status = ST_NEED_DATA; break; }
            if (r->d[p] != 0xFF || r->d[p + 1] != (0xD0 | r->s.next_rst)) { status = ST_ERROR; err = E_RST; break; }
            r->s.pos = p + 2;
            r->s.left = 0;
            r->s.pred[0] = r->s.pred[1] = r->s.pred[2] = r->s.pred[3] = 0;
            r->s.next_rst = (r->s.next_rst + 1) & 7;
            r->s.to_restart = restart;
        }
        int16_t *o = out + done * S->blocks_per_mcu;
        int ci, bi, fail = 0;
        for (ci = 0; ci < S->ncomp && !fail; ci++) {
            const HT *dt = &S->tabs[S->dctab[ci]], *at = &S->tabs[4 + S->actab[ci]];
            for (bi = 0; bi < S->nblocks[ci]; bi++) {
                int s = huff(r, dt), k;
                if (s < 0) { if (s == -2) err = E_HUFF; fail = 1; break; }
                if (s > 11) { err = E_SIZE; fail = 1; break; }
                if (s) {
                    int v = get_bits(r, s);
                    if (v < 0) { fail = 1; break; }
                    if (v < (1 << (s - 1))) v -= (1 << s) - 1;
                    r->s.pred[ci] += v;
                }
                if (r->s.pred[ci] > S->dcbound[ci] || r->s.pred[ci] < -S->dcbound[ci]) { err = E_DCRANGE; fail = 1; break; }
                *o++ = (int16_t)r->s.pred[ci];
                k = 1;
                while (k < 64) {
                    int rs = huff(r, at), rn, sz;
                    if (rs < 0) { if (rs == -2) err = E_HUFF; fail = 1; break; }
                    rn = rs >> 4; sz = rs & 15;
                    if (sz == 0) {
                        if (rn != 15) break;          /* end of block */
                        k += 16;
                        if (k > 63) { err = E_OVERRUN; fail = 1; break; }
                        continue;
                    }
                    k += rn;
                    if (k > 63) { err = E_OVERRUN; fail = 1; break; }
                    if (sz > 10) { err = E_SIZE; fail = 1; break; }
                    if (get_bits(r, sz) < 0) { fail = 1; break; }
                    k++;
                }
                if (fail) break;
            }
        }
        if (fail) {
            if (err == E_NONE && r->marker) err = E_MARKER;
            status = err != E_NONE ? ST_ERROR : ST_NEED_DATA;   /* plain end of data: roll back */
            break;
        }
        r->s.mcu++;
        if (restart > 0) r->s.to_restart--;
        done++;
    }
    r->s = good;
    *status_out = status;
    *err_out = err;
    return done;
}

static PyObject *scan_decode(PyObject *self, PyObject *args) {
    Py_buffer data, statebuf, specbuf;
    long long max_mcus;
    if (!PyArg_ParseTuple(args, "y*y*y*L", &data, &statebuf, &specbuf, &max_mcus))
        return NULL;
    PyObject *result = NULL;
    int16_t *out = NULL;
    Spec *S = (Spec *)PyMem_Malloc(sizeof(Spec));
    if (!S) { PyErr_NoMemory(); goto cleanup; }
    if (parse_spec(S, (const uint8_t *)specbuf.buf, specbuf.len) < 0) goto cleanup;
    if (statebuf.len != (Py_ssize_t)sizeof(State)) { PyErr_SetString(PyExc_ValueError, "bad state size"); goto cleanup; }
    Reader r;
    r.d = (const uint8_t *)data.buf;
    r.end = data.len;
    r.eod = r.marker = 0;
    memcpy(&r.s, statebuf.buf, sizeof(State));
    if (r.s.pos < 0 || r.s.pos > r.end || r.s.left < 0 || r.s.left > 7) {
        PyErr_SetString(PyExc_ValueError, "bad state"); goto cleanup;
    }
    int64_t todo = mcu_cap(&r, S, max_mcus);
    out = (int16_t *)PyMem_Malloc((size_t)(todo > 0 ? todo : 1) * S->blocks_per_mcu * sizeof(int16_t));
    if (!out) { PyErr_NoMemory(); goto cleanup; }
    int status, err;
    int64_t done;
    Py_BEGIN_ALLOW_THREADS
    done = run(&r, S, out, todo, &status, &err);
    Py_END_ALLOW_THREADS
    result = Py_BuildValue("(iLy#y#i)", status, (long long)done,
                           (const char *)out, (Py_ssize_t)(done * S->blocks_per_mcu * sizeof(int16_t)),
                           (const char *)&r.s, (Py_ssize_t)sizeof(State), err);
cleanup:
    if (out) PyMem_Free(out);
    if (S) PyMem_Free(S);
    PyBuffer_Release(&data);
    PyBuffer_Release(&statebuf);
    PyBuffer_Release(&specbuf);
    return result;
}

/* Try many candidate clusters as the continuation of one stream.
 * probe_many(image, data_off, cluster_size, clusters_int64, carry, state, spec, want)
 * want: 0 = keep candidates that decode with no error, 1 = keep only those that finish the picture.
 * Returns a list of (cluster, status, mcus, dc_bytes) for the candidates kept. */
static PyObject *scan_probe_many(PyObject *self, PyObject *args) {
    Py_buffer image, clusters, carry, statebuf, specbuf;
    long long data_off, cl;
    int want;
    if (!PyArg_ParseTuple(args, "y*LLy*y*y*y*i", &image, &data_off, &cl, &clusters, &carry, &statebuf, &specbuf, &want))
        return NULL;
    PyObject *result = NULL;
    uint8_t *buf = NULL;
    int16_t *out = NULL;
    Spec *S = (Spec *)PyMem_Malloc(sizeof(Spec));
    if (!S) { PyErr_NoMemory(); goto cleanup; }
    if (parse_spec(S, (const uint8_t *)specbuf.buf, specbuf.len) < 0) goto cleanup;
    if (statebuf.len != (Py_ssize_t)sizeof(State) || cl <= 0 || data_off < 0) {
        PyErr_SetString(PyExc_ValueError, "bad arguments"); goto cleanup;
    }
    State s0;
    memcpy(&s0, statebuf.buf, sizeof(State));
    s0.pos = 0;
    if (s0.left < 0 || s0.left > 7) { PyErr_SetString(PyExc_ValueError, "bad state"); goto cleanup; }
    Py_ssize_t blen = carry.len + (Py_ssize_t)cl;
    buf = (uint8_t *)PyMem_Malloc((size_t)blen);
    int64_t cap = ((int64_t)blen * 8 + 8) / S->blocks_per_mcu + 1;
    out = (int16_t *)PyMem_Malloc((size_t)cap * S->blocks_per_mcu * sizeof(int16_t));
    if (!buf || !out) { PyErr_NoMemory(); goto cleanup; }
    memcpy(buf, carry.buf, (size_t)carry.len);
    result = PyList_New(0);
    if (!result) goto cleanup;
    Py_ssize_t n = clusters.len / 8, i;
    const int64_t *cs = (const int64_t *)clusters.buf;
    for (i = 0; i < n; i++) {
        int64_t c = cs[i];
        int64_t o = data_off + (c - 2) * cl;
        if (c < 2 || o < 0 || o + cl > image.len) continue;
        memcpy(buf + carry.len, (const uint8_t *)image.buf + o, (size_t)cl);
        Reader r;
        r.d = buf; r.end = blen; r.eod = r.marker = 0; r.s = s0;
        int status, err;
        int64_t todo = S->total - r.s.mcu;
        if (todo > cap) todo = cap;
        int64_t done = run(&r, S, out, todo, &status, &err);
        if (status == ST_ERROR) continue;
        if (want == 1 && status != ST_DONE) continue;
        PyObject *item = Py_BuildValue("(LiLy#)", (long long)c, status, (long long)done,
                                       (const char *)out, (Py_ssize_t)(done * S->blocks_per_mcu * sizeof(int16_t)));
        if (!item || PyList_Append(result, item) < 0) { Py_XDECREF(item); Py_CLEAR(result); goto cleanup; }
        Py_DECREF(item);
    }
cleanup:
    if (buf) PyMem_Free(buf);
    if (out) PyMem_Free(out);
    if (S) PyMem_Free(S);
    PyBuffer_Release(&image); PyBuffer_Release(&clusters); PyBuffer_Release(&carry);
    PyBuffer_Release(&statebuf); PyBuffer_Release(&specbuf);
    return result;
}

static PyMethodDef methods[] = {
    {"decode", scan_decode, METH_VARARGS,
     "decode(data, state, spec, max_mcus) -> (status, mcus, dc_int16_bytes, state, error)"},
    {"probe_many", scan_probe_many, METH_VARARGS,
     "probe_many(image, data_off, cluster_size, clusters, carry, state, spec, want) -> [(cluster, status, mcus, dc)]"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef moddef = {PyModuleDef_HEAD_INIT, "_scan", NULL, -1, methods};

PyMODINIT_FUNC PyInit__scan(void) { return PyModule_Create(&moddef); }
