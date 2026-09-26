/* Nonzero-winding coverage of polygon edges: the C version of gerber._coverage_numpy.

   Same method and output: `samples` rows per pixel, each crossing the edges at its
   centre; between crossings where the running winding is nonzero the row is inside,
   and those spans are added with exact horizontal coverage. The numpy version sorts
   every crossing of the image at once; here only the edges active on a row are kept,
   in x order from the row before, so each row's sort is nearly free.

   Built by tools/build_native.py; loaded through ctypes (no Python headers, so one
   library serves every Python version). Only malloc/free/qsort/memset are used. */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#define KLS_EXPORT __declspec(dllexport)
#else
#define KLS_EXPORT __attribute__((visibility("default")))
#endif

#define KLS_ABI 1

typedef struct {
    double x;       /* crossing on the current row */
    int32_t edge;
} Crossing;

static int by_x(const void *a, const void *b)
{
    double left = ((const Crossing *)a)->x, right = ((const Crossing *)b)->x;
    return (left > right) - (left < right);
}

static void add_edge(double *delta, double position, double weight)
{
    double column = floor(position), fraction = position - column;
    int64_t index = (int64_t)column;
    delta[index] += weight * (1.0 - fraction);
    delta[index + 1] += weight * fraction;
}

KLS_EXPORT int kls_abi(void) { return KLS_ABI; }

/* `edges`: count x (x0, y0, x1, y1) in pixel units, row 0 on top. `out`: height x
   width floats, overwritten with coverage 0..1. Returns 0, or -1 when out of memory. */
KLS_EXPORT int kls_coverage(const double *edges, int64_t count, int32_t width, int32_t height,
                            int32_t samples, float *out)
{
    const int64_t rows = (int64_t)height * samples, stride = (int64_t)width + 2;
    int64_t *first = malloc(sizeof(int64_t) * (count ? count : 1));
    int64_t *stop = malloc(sizeof(int64_t) * (count ? count : 1));
    int64_t *bucket = calloc((size_t)rows + 2, sizeof(int64_t));  /* edges starting on each row */
    int32_t *order = malloc(sizeof(int32_t) * (count ? count : 1));
    Crossing *active = malloc(sizeof(Crossing) * (count ? count : 1));
    double *delta = calloc((size_t)stride, sizeof(double));
    int status = -1;
    if (!first || !stop || !bucket || !order || !active || !delta)
        goto done;

    /* Rows j with j + 0.5 in [low, high), as the numpy version counts them. */
    for (int64_t e = 0; e < count; e++) {
        double y0 = edges[4 * e + 1] * samples, y1 = edges[4 * e + 3] * samples;
        double low = y0 < y1 ? y0 : y1, high = y0 < y1 ? y1 : y0;
        double a = ceil(low - 0.5), b = ceil(high - 0.5);
        a = a < 0 ? 0 : a > (double)rows ? (double)rows : a;
        b = b < 0 ? 0 : b > (double)rows ? (double)rows : b;
        first[e] = (int64_t)a;
        stop[e] = (int64_t)b;
        if (stop[e] > first[e])
            bucket[first[e] + 1]++;
    }
    for (int64_t r = 0; r < rows; r++)
        bucket[r + 1] += bucket[r];
    for (int64_t e = 0; e < count; e++)
        if (stop[e] > first[e])
            order[bucket[first[e]]++] = (int32_t)e;  /* bucket[r] ends as the start of row r + 1 */

    int64_t active_count = 0, next = 0;
    int touched = 0;  /* any span on the current pixel row */
    memset(out, 0, sizeof(float) * (size_t)width * (size_t)height);
    for (int64_t r = 0; r < rows; r++) {
        int64_t kept = 0;
        for (int64_t i = 0; i < active_count; i++)
            if (stop[active[i].edge] > r)
                active[kept++] = active[i];
        int64_t added = 0;
        for (; next < bucket[r]; next++, added++)
            active[kept++].edge = order[next];
        active_count = kept;

        for (int64_t i = 0; i < active_count; i++) {
            const double *edge = edges + 4 * (int64_t)active[i].edge;
            double y0 = edge[1] * samples, y1 = edge[3] * samples;
            active[i].x = edge[0] + (r + 0.5 - y0) * ((edge[2] - edge[0]) / (y1 != y0 ? y1 - y0 : 1.0));
        }
        if (added > 32) {
            qsort(active, (size_t)active_count, sizeof(Crossing), by_x);
        } else {  /* nearly sorted: last row's order plus a few new edges */
            for (int64_t i = 1; i < active_count; i++) {
                Crossing moving = active[i];
                int64_t j = i - 1;
                for (; j >= 0 && active[j].x > moving.x; j--)
                    active[j + 1] = active[j];
                active[j + 1] = moving;
            }
        }

        /* Closed contours cross every row a net zero times. */
        int winding = 0;
        const double weight = 1.0 / samples;
        for (int64_t i = 0; i + 1 < active_count; i++) {
            const double *edge = edges + 4 * (int64_t)active[i].edge;
            winding += edge[3] > edge[1] ? 1 : -1;
            if (winding) {
                double left = active[i].x, right = active[i + 1].x;
                left = left < 0 ? 0 : left > width ? width : left;
                right = right < 0 ? 0 : right > width ? width : right;
                add_edge(delta, left, weight);
                add_edge(delta, right, -weight);
                touched = 1;
            }
        }

        if ((r + 1) % samples == 0 && touched) {
            float *row = out + (r / samples) * (int64_t)width;
            double sum = 0;
            for (int32_t c = 0; c < width; c++) {
                sum += delta[c];
                row[c] = (float)(sum < 0 ? 0 : sum > 1 ? 1 : sum);
            }
            memset(delta, 0, sizeof(double) * (size_t)stride);
            touched = 0;
        }
    }
    status = 0;

done:
    free(first);
    free(stop);
    free(bucket);
    free(order);
    free(active);
    free(delta);
    return status;
}
