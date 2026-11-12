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

#define FAST_BITS 9

typedef struct {
    int32_t mincode[17], maxcode[18], valptr[17];
    uint8_t vals[256];
    uint16_t fast[1 << FAST_BITS];   /* (length << 8) | symbol for codes up to FAST_BITS long; 0 = look further */
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
    int code = 0, k = 0, l, i;
    t->present = 0;
    memset(t->fast, 0, sizeof(t->fast));
    for (l = 1; l <= 16; l++) {
        int n = raw[l - 1];
        if (k + n > 256) n = 256 - k;        /* a damaged table cannot name more than 256 symbols */
        t->valptr[l] = k;
        t->mincode[l] = code;
        if (n) t->present = 1;
        if (l <= FAST_BITS) {
            for (i = 0; i < n; i++) {
                int c = code + i, j, span = 1 << (FAST_BITS - l);
                if (c >> l) break;           /* does not fit in l bits: can never be matched */
                for (j = 0; j < span; j++) {
                    uint16_t *e = &t->fast[(c << (FAST_BITS - l)) + j];
                    if (!*e) *e = (uint16_t)((l << 8) | raw[16 + k + i]);   /* the shortest match wins */
                }
            }
        }
        code += n;
        k += n;
        t->maxcode[l] = n ? code - 1 : -1;
        code <<= 1;
    }
    t->maxcode[17] = 0x7fffffff;
    memcpy(t->vals, raw + 16, 256);
}

/* Bits are taken from a 64-bit window that is refilled a byte at a time (dropping the zero
 * that follows every FF in JPEG data). The window only ever holds bytes that the one-bit-
 * at-a-time rule would also have accepted, so running out means exactly what it meant there:
 * the data ended, or a marker is next. */
typedef struct {
    uint64_t acc;        /* the low nbits bits are unread */
    int nbits;
    int64_t pos;         /* next byte to take into the window */
    int stop;            /* 0, or why no more can be taken: 1 = end of data, 2 = a marker */
    int64_t pos0;        /* where this window started reading */
    int32_t cur0;        /* the partly-read byte it started with */
} Bits;

static inline void bits_init(Bits *b, const State *s) {
    b->nbits = s->left;
    b->acc = s->left ? (uint64_t)(s->cur & ((1 << s->left) - 1)) : 0;
    b->pos = b->pos0 = s->pos;
    b->cur0 = s->cur;
    b->stop = 0;
}

static inline void bits_fill(Bits *b, const uint8_t *d, int64_t end) {
    while (b->nbits <= 32 && !b->stop && b->pos + 4 <= end) {     /* four plain bytes at once */
        const uint8_t *q = d + b->pos;
        if (q[0] == 0xFF || q[1] == 0xFF || q[2] == 0xFF || q[3] == 0xFF) break;
        b->acc = (b->acc << 32) | ((uint64_t)q[0] << 24 | (uint64_t)q[1] << 16 | (uint64_t)q[2] << 8 | (uint64_t)q[3]);
        b->nbits += 32;
        b->pos += 4;
    }
    while (b->nbits <= 56 && !b->stop) {
        int64_t p = b->pos;
        int v;
        if (p >= end) { b->stop = 1; break; }
        v = d[p];
        if (v == 0xFF) {
            if (p + 1 >= end) { b->stop = 1; break; }
            if (d[p + 1] != 0) { b->stop = 2; break; }
            b->pos = p + 2;
        } else {
            b->pos = p + 1;
        }
        b->acc = (b->acc << 8) | (uint64_t)v;
        b->nbits += 8;
    }
}

/* Write the window's position back as (pos, cur, left): whole unread bytes are handed back. */
static void bits_to_state(const Bits *b, State *s, const uint8_t *d) {
    int k = b->nbits >> 3;
    int64_t p = b->pos;
    while (k-- > 0) {
        if (p - 2 >= b->pos0 && d[p - 1] == 0 && d[p - 2] == 0xFF) p -= 2; else p -= 1;
    }
    s->pos = p;
    s->left = b->nbits & 7;
    if (p == b->pos0) s->cur = b->cur0;
    else if (p - 2 >= b->pos0 && d[p - 1] == 0 && d[p - 2] == 0xFF) s->cur = 0xFF;
    else s->cur = d[p - 1];
}

static inline int take_bits(Bits *b, const uint8_t *d, int64_t end, int n) {
    if (b->nbits < n) {
        bits_fill(b, d, end);
        if (b->nbits < n) return -1;
    }
    b->nbits -= n;
    return (int)((b->acc >> b->nbits) & ((1u << n) - 1));
}

/* Returns the symbol, -1 if the data ends or a marker comes first, -2 on an invalid code. */
static inline int take_symbol(Bits *b, const uint8_t *d, int64_t end, const HT *t) {
    int nb, code, l;
    unsigned e;
    if (b->nbits < 16) bits_fill(b, d, end);
    nb = b->nbits;
    e = t->fast[nb >= FAST_BITS ? (unsigned)(b->acc >> (nb - FAST_BITS)) & ((1u << FAST_BITS) - 1)
                                : (unsigned)(b->acc << (FAST_BITS - nb)) & ((1u << FAST_BITS) - 1)];
    if (e) {
        l = (int)(e >> 8);
        if (l > nb) return -1;
        b->nbits = nb - l;
        return (int)(e & 255);
    }
    for (l = 1; l <= 16; l++) {
        if (l > nb) return -1;
        code = (int)((b->acc >> (nb - l)) & ((1u << l) - 1));
        if (t->maxcode[l] >= 0 && code <= t->maxcode[l] && code >= t->mincode[l]) {
            b->nbits = nb - l;
            return t->vals[t->valptr[l] + code - t->mincode[l]];
        }
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
    const int restart = S->restart;
    const uint8_t *d = r->d;
    const int64_t end = r->end;
    State st = r->s, good_st;
    Bits b, good_b;
    bits_init(&b, &st);
    while (1) {
        good_b = b; good_st = st;
        if (st.mcu >= S->total) { status = ST_DONE; break; }
        if (done >= todo) { status = ST_NEED_DATA; break; }
        if (restart > 0 && st.to_restart == 0) {
            /* a restart marker is due: drop padding bits, then expect FF Dn */
            int64_t p;
            bits_to_state(&b, &st, d);
            p = st.pos;
            while (p + 1 < end && d[p] == 0xFF && d[p + 1] == 0xFF) p++;
            if (p + 1 >= end) { status = ST_NEED_DATA; break; }
            if (d[p] != 0xFF || d[p + 1] != (0xD0 | st.next_rst)) { status = ST_ERROR; err = E_RST; break; }
            st.pos = p + 2;
            st.left = 0;
            st.pred[0] = st.pred[1] = st.pred[2] = st.pred[3] = 0;
            st.next_rst = (st.next_rst + 1) & 7;
            st.to_restart = restart;
            bits_init(&b, &st);
        }
        int16_t *o = out + done * S->blocks_per_mcu;
        int ci, bi, fail = 0;
        for (ci = 0; ci < S->ncomp && !fail; ci++) {
            const HT *dt = &S->tabs[S->dctab[ci]], *at = &S->tabs[4 + S->actab[ci]];
            for (bi = 0; bi < S->nblocks[ci]; bi++) {
                int s = take_symbol(&b, d, end, dt), k;
                if (s < 0) { if (s == -2) err = E_HUFF; fail = 1; break; }
                if (s > 11) { err = E_SIZE; fail = 1; break; }
                if (s) {
                    int v = take_bits(&b, d, end, s);
                    if (v < 0) { fail = 1; break; }
                    if (v < (1 << (s - 1))) v -= (1 << s) - 1;
                    st.pred[ci] += v;
                }
                if (st.pred[ci] > S->dcbound[ci] || st.pred[ci] < -S->dcbound[ci]) { err = E_DCRANGE; fail = 1; break; }
                *o++ = (int16_t)st.pred[ci];
                k = 1;
                while (k < 64) {
                    int rs = take_symbol(&b, d, end, at), rn, sz;
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
                    if (take_bits(&b, d, end, sz) < 0) { fail = 1; break; }
                    k++;
                }
                if (fail) break;
            }
        }
        if (fail) {
            if (err == E_NONE && b.stop == 2) err = E_MARKER;
            status = err != E_NONE ? ST_ERROR : ST_NEED_DATA;   /* plain end of data: roll back */
            break;
        }
        st.mcu++;
        if (restart > 0) st.to_restart--;
        done++;
    }
    bits_to_state(&good_b, &good_st, d);
    r->s = good_st;
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

/* A fast first look at many candidate clusters: decode only the first n_mcus MCUs of each.
 * screen_many(image, data_off, cluster_size, clusters_int64, carry, state, spec, n_mcus)
 * Returns (full_ids, dc, short_ids): full_ids (int64 bytes) decoded n_mcus MCUs cleanly and
 * their DC values follow in dc, n_mcus * blocks int16 each; short_ids decoded cleanly but
 * yielded fewer MCUs (the picture ends, or the cluster does). Candidates that stop being
 * valid JPEG are dropped. Runs without the interpreter lock. */
static PyObject *scan_screen_many(PyObject *self, PyObject *args) {
    Py_buffer image, clusters, carry, statebuf, specbuf;
    long long data_off, cl;
    int n_mcus;
    if (!PyArg_ParseTuple(args, "y*LLy*y*y*y*i", &image, &data_off, &cl, &clusters, &carry, &statebuf, &specbuf, &n_mcus))
        return NULL;
    PyObject *result = NULL;
    uint8_t *buf = NULL;
    int16_t *out = NULL, *dcs = NULL;
    int64_t *full = NULL, *shrt = NULL;
    Py_ssize_t n = clusters.len / 8, i, nfull = 0, nshort = 0;
    Spec *S = (Spec *)PyMem_Malloc(sizeof(Spec));
    if (!S) { PyErr_NoMemory(); goto cleanup; }
    if (parse_spec(S, (const uint8_t *)specbuf.buf, specbuf.len) < 0) goto cleanup;
    if (statebuf.len != (Py_ssize_t)sizeof(State) || cl <= 0 || data_off < 0 || n_mcus < 1 || n_mcus > 4096) {
        PyErr_SetString(PyExc_ValueError, "bad arguments"); goto cleanup;
    }
    State s0;
    memcpy(&s0, statebuf.buf, sizeof(State));
    s0.pos = 0;
    if (s0.left < 0 || s0.left > 7) { PyErr_SetString(PyExc_ValueError, "bad state"); goto cleanup; }
    Py_ssize_t blen = carry.len + (Py_ssize_t)cl;
    size_t per = (size_t)n_mcus * S->blocks_per_mcu;
    buf = (uint8_t *)PyMem_Malloc((size_t)blen);
    out = (int16_t *)PyMem_Malloc((per + S->blocks_per_mcu) * sizeof(int16_t));
    dcs = (int16_t *)PyMem_Malloc((size_t)(n ? n : 1) * per * sizeof(int16_t));
    full = (int64_t *)PyMem_Malloc((size_t)(n ? n : 1) * sizeof(int64_t));
    shrt = (int64_t *)PyMem_Malloc((size_t)(n ? n : 1) * sizeof(int64_t));
    if (!buf || !out || !dcs || !full || !shrt) { PyErr_NoMemory(); goto cleanup; }
    memcpy(buf, carry.buf, (size_t)carry.len);
    const int64_t *cs = (const int64_t *)clusters.buf;
    const uint8_t *img = (const uint8_t *)image.buf;
    Py_ssize_t ilen = image.len, clen = carry.len;
    Py_BEGIN_ALLOW_THREADS
    for (i = 0; i < n; i++) {
        int64_t c = cs[i];
        int64_t o = data_off + (c - 2) * cl;
        if (c < 2 || o < 0 || o + cl > ilen) continue;
        memcpy(buf + clen, img + o, (size_t)cl);
        Reader r;
        r.d = buf; r.end = blen; r.eod = r.marker = 0; r.s = s0;
        int status, err;
        int64_t todo = S->total - r.s.mcu;
        if (todo > n_mcus) todo = n_mcus;
        int64_t done = run(&r, S, out, todo, &status, &err);
        if (status == ST_ERROR) continue;
        if (done == n_mcus) {
            memcpy(dcs + (size_t)nfull * per, out, per * sizeof(int16_t));
            full[nfull++] = c;
        } else {
            shrt[nshort++] = c;
        }
    }
    Py_END_ALLOW_THREADS
    result = Py_BuildValue("(y#y#y#)", (const char *)full, (Py_ssize_t)(nfull * sizeof(int64_t)),
                           (const char *)dcs, (Py_ssize_t)((size_t)nfull * per * sizeof(int16_t)),
                           (const char *)shrt, (Py_ssize_t)(nshort * sizeof(int64_t)));
cleanup:
    if (buf) PyMem_Free(buf);
    if (out) PyMem_Free(out);
    if (dcs) PyMem_Free(dcs);
    if (full) PyMem_Free(full);
    if (shrt) PyMem_Free(shrt);
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
    {"screen_many", scan_screen_many, METH_VARARGS,
     "screen_many(image, data_off, cluster_size, clusters, carry, state, spec, n_mcus) -> (full_ids, dc, short_ids)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef moddef = {PyModuleDef_HEAD_INIT, "_scan", NULL, -1, methods};

PyMODINIT_FUNC PyInit__scan(void) { return PyModule_Create(&moddef); }
