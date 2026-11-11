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

static PyObject *scan_decode(PyObject *self, PyObject *args) {
    Py_buffer data, statebuf, specbuf;
    long long max_mcus;
    if (!PyArg_ParseTuple(args, "y*y*y*L", &data, &statebuf, &specbuf, &max_mcus))
        return NULL;

    PyObject *result = NULL;
    int16_t *out = NULL;
    HT *tabs = NULL;
    int32_t ncomp, nblocks[4], dctab[4], actab[4], dcbound[4], restart;
    int64_t total;
    const uint8_t *sp = (const uint8_t *)specbuf.buf;
    Py_ssize_t need = 4 + 4 * 16 + 4 + 8 + 8 * 272;
    int i, blocks_per_mcu = 0;

    if (specbuf.len != need || statebuf.len != (Py_ssize_t)sizeof(State)) {
        PyErr_SetString(PyExc_ValueError, "bad spec or state size");
        goto cleanup;
    }
    memcpy(&ncomp, sp, 4); sp += 4;
    if (ncomp < 1 || ncomp > 4) { PyErr_SetString(PyExc_ValueError, "bad component count"); goto cleanup; }
    for (i = 0; i < 4; i++) {
        memcpy(&nblocks[i], sp, 4); memcpy(&dctab[i], sp + 4, 4);
        memcpy(&actab[i], sp + 8, 4); memcpy(&dcbound[i], sp + 12, 4);
        sp += 16;
        if (i < ncomp) {
            if (nblocks[i] < 1 || nblocks[i] > 16 || dctab[i] < 0 || dctab[i] > 3 || actab[i] < 0 || actab[i] > 3) {
                PyErr_SetString(PyExc_ValueError, "bad component spec"); goto cleanup;
            }
            blocks_per_mcu += nblocks[i];
        }
    }
    memcpy(&restart, sp, 4); sp += 4;
    memcpy(&total, sp, 8); sp += 8;
    tabs = (HT *)PyMem_Malloc(8 * sizeof(HT));
    if (!tabs) { PyErr_NoMemory(); goto cleanup; }
    for (i = 0; i < 8; i++) build_table(&tabs[i], sp + i * 272);

    Reader r;
    r.d = (const uint8_t *)data.buf;
    r.end = data.len;
    r.eod = r.marker = 0;
    memcpy(&r.s, statebuf.buf, sizeof(State));
    if (r.s.pos < 0 || r.s.pos > r.end || r.s.left < 0 || r.s.left > 7) {
        PyErr_SetString(PyExc_ValueError, "bad state"); goto cleanup;
    }

    int64_t todo = total - r.s.mcu;
    if (max_mcus >= 0 && max_mcus < todo) todo = max_mcus;
    if (todo < 0) todo = 0;
    /* an MCU needs at least one bit per block, so the data bounds the output */
    int64_t cap = ((r.end - r.s.pos) * 8 + 8) / (blocks_per_mcu > 0 ? blocks_per_mcu : 1) + 1;
    if (cap < todo) todo = cap;
    out = (int16_t *)PyMem_Malloc((size_t)(todo > 0 ? todo : 1) * blocks_per_mcu * sizeof(int16_t));
    if (!out) { PyErr_NoMemory(); goto cleanup; }

    int status = ST_NEED_DATA, err = E_NONE;
    int64_t done = 0;
    State good = r.s;

    Py_BEGIN_ALLOW_THREADS
    while (1) {
        good = r.s;
        if (r.s.mcu >= total) { status = ST_DONE; break; }
        if (done >= todo) { status = ST_NEED_DATA; break; }
        if (restart > 0 && r.s.to_restart == 0) {
            /* a restart marker is due: drop padding bits, then expect FF Dn */
            int64_t p = r.s.pos;
            while (p + 1 < r.end && r.d[p] == 0xFF && r.d[p + 1] == 0xFF) p++;
            if (p + 1 >= r.end) { status = ST_NEED_DATA; break; }
            if (r.d[p] != 0xFF || r.d[p + 1] != (0xD0 | r.s.next_rst)) { status = ST_ERROR; err = E_RST; break; }
            r.s.pos = p + 2;
            r.s.left = 0;
            r.s.pred[0] = r.s.pred[1] = r.s.pred[2] = r.s.pred[3] = 0;
            r.s.next_rst = (r.s.next_rst + 1) & 7;
            r.s.to_restart = restart;
        }
        int16_t *o = out + done * blocks_per_mcu;
        int ci, bi, fail = 0;
        for (ci = 0; ci < ncomp && !fail; ci++) {
            const HT *dt = &tabs[dctab[ci]], *at = &tabs[4 + actab[ci]];
            for (bi = 0; bi < nblocks[ci]; bi++) {
                int s = huff(&r, dt), k;
                if (s < 0) { if (s == -2) err = E_HUFF; fail = 1; break; }
                if (s > 11) { err = E_SIZE; fail = 1; break; }
                if (s) {
                    int v = get_bits(&r, s);
                    if (v < 0) { fail = 1; break; }
                    if (v < (1 << (s - 1))) v -= (1 << s) - 1;
                    r.s.pred[ci] += v;
                }
                if (r.s.pred[ci] > dcbound[ci] || r.s.pred[ci] < -dcbound[ci]) { err = E_DCRANGE; fail = 1; break; }
                *o++ = (int16_t)r.s.pred[ci];
                k = 1;
                while (k < 64) {
                    int rs = huff(&r, at), run, sz;
                    if (rs < 0) { if (rs == -2) err = E_HUFF; fail = 1; break; }
                    run = rs >> 4; sz = rs & 15;
                    if (sz == 0) {
                        if (run != 15) break;          /* end of block */
                        k += 16;
                        if (k > 63) { err = E_OVERRUN; fail = 1; break; }
                        continue;
                    }
                    k += run;
                    if (k > 63) { err = E_OVERRUN; fail = 1; break; }
                    if (sz > 10) { err = E_SIZE; fail = 1; break; }
                    if (get_bits(&r, sz) < 0) { fail = 1; break; }
                    k++;
                }
                if (fail) break;
            }
        }
        if (fail) {
            if (err == E_NONE && r.marker) err = E_MARKER;
            if (err != E_NONE) { status = ST_ERROR; }
            else { status = ST_NEED_DATA; }          /* plain end of data: roll back */
            r.s = good;
            break;
        }
        r.s.mcu++;
        if (restart > 0) r.s.to_restart--;
        done++;
    }
    Py_END_ALLOW_THREADS

    if (status != ST_ERROR) r.s = good;
    result = Py_BuildValue("(iLy#y#i)", status, (long long)done,
                           (const char *)out, (Py_ssize_t)(done * blocks_per_mcu * sizeof(int16_t)),
                           (const char *)&r.s, (Py_ssize_t)sizeof(State), err);

cleanup:
    if (out) PyMem_Free(out);
    if (tabs) PyMem_Free(tabs);
    PyBuffer_Release(&data);
    PyBuffer_Release(&statebuf);
    PyBuffer_Release(&specbuf);
    return result;
}

static PyMethodDef methods[] = {
    {"decode", scan_decode, METH_VARARGS,
     "decode(data, state, spec, max_mcus) -> (status, mcus, dc_int16_bytes, state, error)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef moddef = {PyModuleDef_HEAD_INIT, "_scan", NULL, -1, methods};

PyMODINIT_FUNC PyInit__scan(void) { return PyModule_Create(&moddef); }
