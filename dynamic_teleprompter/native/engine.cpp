// Dynamic Teleprompter native processing engine — ABI v1.
// First migrated operation: BGRA8 hard chroma key.
// This is deliberately NOT a GPU renderer or a VST/Live2D host.
#include <cstdint>
#include <cstddef>

#if defined(_WIN32)
#define DT_EXPORT extern "C" __declspec(dllexport)
#else
#define DT_EXPORT extern "C" __attribute__((visibility("default")))
#endif

DT_EXPORT int dt_engine_abi_version() noexcept { return 1; }

// BGRA8 with possibly padded row stride. Returns 0 on success.
// Upper color bound and lower bound are inclusive and clamped to [0,255].
// Color component order is B,G,R. Pixel alpha is set to 0 for matching pixels.
DT_EXPORT int dt_chroma_key_bgra(uint8_t* pixels, int width, int height,
                                 int stride, int key_b, int key_g, int key_r,
                                 int tolerance) noexcept {
    if (!pixels || width <= 0 || height <= 0 || width > 16384 || height > 16384 ||
        stride < width * 4 || stride > 16384 * 8 || tolerance < 0 || tolerance > 255 ||
        key_b < 0 || key_b > 255 || key_g < 0 || key_g > 255 || key_r < 0 || key_r > 255)
        return -1;
    const int keys[] = {key_b, key_g, key_r};
    int lower[3], upper[3];
    for (int c = 0; c < 3; ++c) {
        lower[c] = keys[c] > tolerance ? keys[c] - tolerance : 0;
        upper[c] = keys[c] + tolerance < 255 ? keys[c] + tolerance : 255;
    }
    for (int y = 0; y < height; ++y) {
        uint8_t* row = pixels + static_cast<size_t>(y) * stride;
        for (int x = 0; x < width; ++x) {
            uint8_t* pixel = row + static_cast<size_t>(x) * 4;
            if (pixel[0] >= lower[0] && pixel[0] <= upper[0] &&
                pixel[1] >= lower[1] && pixel[1] <= upper[1] &&
                pixel[2] >= lower[2] && pixel[2] <= upper[2])
                pixel[3] = 0;
        }
    }
    return 0;
}

// BGRA8 color correction. Saturation=100 and contrast/brightness=0 mean no-op.
// Based on the same BT.601 luma coefficients and order as the NumPy fallback.
// Caller owns the buffer; processing is synchronous and in-place.
#include <cmath>
DT_EXPORT int dt_color_adjust_bgra(uint8_t* pixels, int width, int height,
                                   int stride, int brightness, int contrast,
                                   int saturation) noexcept {
    if (!pixels || width <= 0 || height <= 0 || width > 16384 || height > 16384 ||
        stride < width * 4 || stride > 16384 * 8 || brightness < -100 || brightness > 100 ||
        contrast < -100 || contrast > 100 || saturation < 0 || saturation > 200) return -1;
    const float sat = saturation / 100.0f;
    const float con = 1.0f + contrast / 100.0f;
    const float bright = brightness * 2.55f;
    for (int y = 0; y < height; ++y) {
        uint8_t* row = pixels + static_cast<size_t>(y) * stride;
        for (int x = 0; x < width; ++x) {
            uint8_t* v = row + static_cast<size_t>(x) * 4;
            float b = v[0], g = v[1], r = v[2];
            const float gray = b * 0.114f + g * 0.587f + r * 0.299f;
            float c[3] = {b, g, r};
            for (int i = 0; i < 3; ++i) {
                float value = c[i];
                if (saturation != 100) value = gray + (value - gray) * sat;
                if (contrast != 0) value = (value - 128.0f) * con + 128.0f;
                if (brightness != 0) value += bright;
                value = value < 0.0f ? 0.0f : (value > 255.0f ? 255.0f : value);
                v[i] = static_cast<uint8_t>(value);
            }
        }
    }
    return 0;
}


// Audio DSP milestone: mono signed 16-bit PCM at an arbitrary sample rate.
// In-place gain + hard noise gate. No allocations, exceptions, or I/O.
// Gate threshold is in sample amplitude [0,32767]; gain in integer percent [0,400].
// Designed for recorder-side processing; this is not yet a real-time VST host.
DT_EXPORT int dt_audio_process_s16(int16_t* samples, int sample_count,
                                   int gain_percent, int gate_threshold,
                                   int* peak_out) noexcept {
    if (!samples || sample_count < 0 || sample_count > 10000000 ||
        gain_percent < 0 || gain_percent > 400 ||
        gate_threshold < 0 || gate_threshold > 32767 || !peak_out) return -1;
    int peak = 0;
    for (int i = 0; i < sample_count; ++i) {
        const int value = static_cast<int>(samples[i]);
        const int magnitude = value < 0 ? -value : value;
        const int gated = magnitude < gate_threshold ? 0 : value;
        // Integer math gives the same rounding on all supported compilers.
        int adjusted = gated * gain_percent / 100;
        if (adjusted > 32767) adjusted = 32767;
        if (adjusted < -32768) adjusted = -32768;
        samples[i] = static_cast<int16_t>(adjusted);
        const int output_mag = adjusted < 0 ? -adjusted : adjusted;
        if (output_mag > peak) peak = output_mag;
    }
    *peak_out = peak;
    return 0;
}
