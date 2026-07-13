// rand_freeze — force libc rand()/srand() to a fixed, seed-independent sequence.
//
// The Nikon Image SDK drives a dithering stage from rand(), seeded
// non-deterministically each run, so two renders of the same NEF differ by a
// fine noise-like pattern (MAE ~5/255) on some render paths. Interposing rand()
// with a deterministic LCG — and making srand() a no-op so the SDK's own
// (time-based) seed is ignored — pins that dither to a fixed pattern, making
// native macOS output byte-reproducible. This controls run-to-run variance; it
// does not imply pixel equality with NX Studio or another SDK platform.
//
// Loaded by nef_watch.py --deterministic via DYLD_INSERT_LIBRARIES; not linked
// into nef_render itself (two-level namespace wouldn't intercept the SDK's
// calls). Built next to nef_render by build.sh.
#include <stdint.h>
#include <stdlib.h>

#define INTERPOSE(_new, _orig) \
  __attribute__((used)) static struct { const void* n; const void* o; } \
  _ip_##_orig __attribute__((section("__DATA,__interpose"))) = \
  { (const void*)&_new, (const void*)&_orig }

// glibc-style LCG; deterministic and independent of any srand() seed.
static uint32_t g_state = 1u;

static int det_rand(void) {
    g_state = g_state * 1103515245u + 12345u;
    return (int)((g_state >> 16) & 0x7fff);
}
INTERPOSE(det_rand, rand);

static void det_srand(unsigned seed) { (void)seed; g_state = 1u; }  // ignore seed
INTERPOSE(det_srand, srand);

static long det_random(void) {
    g_state = g_state * 1103515245u + 12345u;
    return (long)(g_state & 0x7fffffff);
}
INTERPOSE(det_random, random);

static void det_srandom(unsigned seed) { (void)seed; g_state = 1u; }
INTERPOSE(det_srandom, srandom);
