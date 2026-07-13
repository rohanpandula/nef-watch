// nef_render — headless single-NEF developer for the nef-watch CLI.
//
// Renders one Nikon NEF through the Nikon NEF/NRW Image SDK (camera-matched
// "Applied in Camera" look) and writes the pixels to a raw file the Python
// orchestrator then encodes as TIFF. Writing to a FILE (not stdout) keeps the
// SDK's chatty logging from corrupting the pixel stream.
//
// usage: nef_render <input.nef> <output.raw> <profile.icm> [bits=8] [expcomp_ev=0]
//
// Raw file: ASCII header line "NKRAW1 <w> <h> <ch> <byteDepth> <orient>\n"
// then w*h*ch*byteDepth row-major bytes. byteDepth is the *written* depth (1 or 2).

#import <Cocoa/Cocoa.h>
#include "NkImageLibCtrl.h"
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>

static constexpr std::uint64_t kMaxRenderedPixels = 100000000ULL;
static constexpr std::uint64_t kMaxRenderedBytes = 800000000ULL;

static int channelsForColor(unsigned long ulColor) {
    switch (ulColor) {
        case kNkfl_Color_Gray:     return 1;
        case kNkfl_Color_RGB_Gray: return 3;
        case kNkfl_Color_RGB:      return 3;
        case kNkfl_Color_CMYK:     return 4;
        default:                   return 0;
    }
}

// Human-readable text for Nkfl_Interface.h kNkfl_Code_Err_* values, so SDK
// failures print more than a raw hex code. Verified against Nkfl_Interface.h.
static const char* nkflErrText(unsigned long code) {
    switch (code) {
        case 0x0001: return "out of memory";
        case 0x0002: return "out of resources";
        case 0x0003: return "unsupported operation or file/camera type";
        case 0x0004: return "invalid parameter — corrupt or truncated file (possibly still copying)";
        case 0x0005: return "wrong call sequence";
        case 0x0006: return "SDK support file missing (dylib/prm.bin)";
        case 0x0007: return "SDK version mismatch";
        case 0x0008: return "unexpected SDK error";
        case 0x0009: return "file I/O error";
        case 0x000E: return "invalid tag data";
        case 0x0013: return "no shooting data found in image";
        case 0x0014: return "optional Picture Control is not installed";
        case 0x0101: return "optional Picture Control is not applicable";
        case 0x0102: return "edit state does not exist";
        case 0x0103: return "edit state is not applicable";
        case 0x0104: return "low-resolution mode is not applicable";
        case 0x0105: return "D2X mode is not applicable";
        case 0x0106: return "Auto2 white balance is not applicable";
        case 0x0107: return "ExtraHigh2 Active D-Lighting is not applicable";
        case 0x0108: return "underwater white balance is not applicable";
        case 0x0109: return "Auto1 white balance is not applicable";
        case 0x0110: return "Flat Picture Control is not applicable";
        case 0x0111: return "Auto0 white balance is not applicable";
        case 0x0112: return "Auto Picture Control is not applicable";
        case 0x0113: return "Natural Light Auto white balance is not applicable";
        case 0x0114: return "Creative Picture Control is not applicable";
        case 0x0115: return "HLG Picture Control is not applicable";
        default:     return (code & 0x0F00UL) ? "unrecognized SDK warning" : "SDK error";
    }
}

static bool nkflIsWarning(unsigned long code) {
    return (code & 0x0F00UL) != 0;
}

int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc < 4) {
            fprintf(stderr, "usage: %s <input.nef> <output.raw> <profile.icm> [bits=8] [expcomp_ev=0]\n", argv[0]);
            return 1;
        }
        const char* nefPath = argv[1];
        const char* rawPath = argv[2];
        const char* iccPath = argv[3];
        int    outBits  = (argc >= 5) ? atoi(argv[4]) : 8;     // 8 or 16
        double expComp  = (argc >= 6) ? atof(argv[5]) : 0.0;
        if (outBits != 8 && outBits != 16) outBits = 8;

        NSApplicationLoad();  // init AppKit for headless use (no run loop needed)

        unsigned long err = CImageLibCtrl::OpenLibrary();
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "OpenLibrary failed: %s [0x%04lx]\n", nkflErrText(err), err);
            return 2;
        }

        CImageLibCtrl ctrl;
        err = ctrl.OpenSession((void*)nefPath);
        if (err != kNkfl_Code_None) {
            if (nkflIsWarning(err)) {
                // Nikon can establish a session while substituting a camera
                // setting (for example Standard for a missing optional Picture
                // Control). Exact-color mode must never accept that fallback.
                fprintf(stderr,
                        "OpenSession warning rejected by strict color mode: %s "
                        "[0x%04lx]; Nikon may have substituted an in-camera setting\n",
                        nkflErrText(err), err);
                ctrl.CloseSession();
            } else {
                fprintf(stderr, "OpenSession '%s' failed: %s [0x%04lx]\n",
                        nefPath, nkflErrText(err), err);
            }
            CImageLibCtrl::CloseLibrary();
            return 3;
        }

        // Call order matters (credit: FUSe / r/Nikon). RawParameterSet=AsShot
        // resets session state, so it must come FIRST — before color process,
        // edits, and the output profile. Setting the output profile before this
        // makes the SDK silently revert it to the display ICC (flatter render).
        {
            NkflRawDevelopment_RawParameterSet ps = {0};
            ps.ulSize = sizeof(ps);
            ps.ulParamterSet = kNkfl_RawParameterSet_AsShot;
            err = ctrl.RawDevelopment(kNkfl_RawDevelopment_RawParameterSet, &ps);
            if (err != kNkfl_Code_None) {
                fprintf(stderr, "RawParameterSet(AsShot) failed: %s [0x%04lx]\n",
                        nkflErrText(err), err);
                ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
                return 4;
            }
        }
        // Defensive: with DevelopColorMode=AppliedInCamera this is already the
        // session default (verified), but set it explicitly to stay correct if
        // adjustments are ever added.
        err = ctrl.SetColorProcess(kNkfl_ColorProcess_AppliedInCamera);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "SetColorProcess(AppliedInCamera) failed: %s [0x%04lx]\n",
                    nkflErrText(err), err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 4;
        }

        if (expComp != 0.0) {
            err = ctrl.SetExpComp(expComp);   // edits go after AsShot
            if (err != kNkfl_Code_None) {
                fprintf(stderr, "SetExpComp failed: %s [0x%04lx]\n",
                        nkflErrText(err), err);
                ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
                return 4;
            }
        }

        // Output profile LAST, after AsShot, or it gets reverted to the display ICC.
        err = ctrl.SetOutputProfile(kNkfl_RenderingIntent_Relative, (unsigned char*)iccPath);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "SetOutputProfile failed: %s [0x%04lx]\n",
                    nkflErrText(err), err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 4;
        }

        NkIL_ImageInfo info = {0};
        err = ctrl.GetImageInfo(&info);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "GetImageInfo failed: %s [0x%04lx]\n", nkflErrText(err), err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 4;
        }

        int channels = channelsForColor(info.ulColor);
        unsigned long srcDepth = info.ulByteDepth;           // bytes/sample from SDK (usually 2)
        if (channels == 0 || (srcDepth != 1 && srcDepth != 2)) {
            fprintf(stderr, "unsupported SDK image format: color=0x%lx depth=%lu\n",
                    info.ulColor, srcDepth);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 5;
        }
        if (info.ulWidth == 0 || info.ulHeight == 0 ||
            info.ulWidth > SHRT_MAX || info.ulHeight > SHRT_MAX) {
            fprintf(stderr, "unsafe SDK image dimensions: %lux%lu\n",
                    info.ulWidth, info.ulHeight);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 5;
        }
        std::uint64_t pixels = static_cast<std::uint64_t>(info.ulWidth) * info.ulHeight;
        std::uint64_t nbytes64 = pixels * static_cast<std::uint64_t>(channels) * srcDepth;
        if (pixels > kMaxRenderedPixels || nbytes64 > kMaxRenderedBytes ||
            nbytes64 > std::numeric_limits<size_t>::max() ||
            nbytes64 > std::numeric_limits<unsigned long>::max()) {
            fprintf(stderr, "unsafe SDK image dimensions or buffer size: %lux%lu (%llu bytes)\n",
                    info.ulWidth, info.ulHeight,
                    static_cast<unsigned long long>(nbytes64));
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 5;
        }
        size_t nbytes = static_cast<size_t>(nbytes64);
        std::vector<unsigned char> buf(nbytes);

        NkIL_ImageParam p = {0};
        p.rect.top = 0; p.rect.left = 0;
        p.rect.bottom = (short)info.ulHeight; p.rect.right = (short)info.ulWidth;
        p.ulLength = (unsigned long)nbytes;
        p.pData = buf.data();
        err = ctrl.GetImageData(&p);
        // The SDK sample documents 0x0104 as a successful render where its
        // low-resolution optimization was skipped and full image data returned.
        if (err != kNkfl_Code_None && err != 0x0104) {
            fprintf(stderr, "GetImageData failed: %s [0x%04lx]\n", nkflErrText(err), err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 5;
        }
        if (err == 0x0104) {
            fprintf(stderr, "GetImageData warning: %s [0x%04lx]; using returned full-resolution data\n",
                    nkflErrText(err), err);
        }
        ctrl.CloseSession();
        CImageLibCtrl::CloseLibrary();

        // Decide written depth and convert if needed (16->8 with rounding,
        // matching the validated spike path).
        size_t nsamp = (size_t)info.ulWidth * info.ulHeight * channels;
        unsigned long outDepth = srcDepth;
        std::vector<unsigned char> out;
        if (srcDepth == 2 && outBits == 8) {
            out.resize(nsamp);
            for (size_t i = 0; i < nsamp; ++i) {
                std::uint16_t value = 0;
                std::memcpy(&value, buf.data() + i * sizeof(value), sizeof(value));
                out[i] = (unsigned char)(((unsigned int)value * 255 + 32767) / 65535);
            }
            outDepth = 1;
        } else {
            out.swap(buf);  // write as-is (already 8-bit, or 16-bit requested)
            outDepth = srcDepth;
        }
        size_t outBytes = nsamp * outDepth;

        FILE* f = fopen(rawPath, "wb");
        if (!f) { fprintf(stderr, "open out '%s'\n", rawPath); return 6; }
        fprintf(f, "NKRAW1 %lu %lu %d %lu %lu\n",
                info.ulWidth, info.ulHeight, channels, outDepth, info.ulOrientation);
        size_t wrote = fwrite(out.data(), 1, outBytes, f);
        fclose(f);
        if (wrote != outBytes) { fprintf(stderr, "short write %zu/%zu\n", wrote, outBytes); return 7; }

        printf("OK %lux%lu %dch %lubit orient=%lu\n",
               info.ulWidth, info.ulHeight, channels, outDepth * 8, info.ulOrientation);
        return 0;
    }
}
