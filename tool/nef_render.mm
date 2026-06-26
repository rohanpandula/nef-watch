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
#include <cstdio>
#include <cstdlib>
#include <vector>

static int channelsForColor(unsigned long ulColor) {
    switch (ulColor) {
        case kNkfl_Color_Gray:     return 1;
        case kNkfl_Color_RGB_Gray: return 3;
        case kNkfl_Color_RGB:      return 3;
        case kNkfl_Color_CMYK:     return 4;
        default:                   return 3;
    }
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
        if (err != kNkfl_Code_None) { fprintf(stderr, "OpenLibrary 0x%04lx\n", err); return 2; }

        CImageLibCtrl ctrl;
        err = ctrl.OpenSession((void*)nefPath);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "OpenSession '%s' 0x%04lx\n", nefPath, err);
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
            ctrl.RawDevelopment(kNkfl_RawDevelopment_RawParameterSet, &ps);
        }
        // Defensive: with DevelopColorMode=AppliedInCamera this is already the
        // session default (verified), but set it explicitly to stay correct if
        // adjustments are ever added.
        ctrl.SetColorProcess(kNkfl_ColorProcess_AppliedInCamera);

        if (expComp != 0.0) ctrl.SetExpComp(expComp);   // edits go after AsShot

        // Output profile LAST, after AsShot, or it gets reverted to the display ICC.
        ctrl.SetOutputProfile(kNkfl_RenderingIntent_Relative, (unsigned char*)iccPath);

        NkIL_ImageInfo info = {0};
        err = ctrl.GetImageInfo(&info);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "GetImageInfo 0x%04lx\n", err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 4;
        }

        int channels = channelsForColor(info.ulColor);
        unsigned long srcDepth = info.ulByteDepth;           // bytes/sample from SDK (usually 2)
        size_t nbytes = (size_t)info.ulWidth * info.ulHeight * channels * srcDepth;
        std::vector<unsigned char> buf(nbytes);

        NkIL_ImageParam p = {0};
        p.rect.top = 0; p.rect.left = 0;
        p.rect.bottom = (short)info.ulHeight; p.rect.right = (short)info.ulWidth;
        p.ulLength = (unsigned long)nbytes;
        p.pData = buf.data();
        err = ctrl.GetImageData(&p);
        if (err != kNkfl_Code_None) {
            fprintf(stderr, "GetImageData 0x%04lx\n", err);
            ctrl.CloseSession(); CImageLibCtrl::CloseLibrary();
            return 5;
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
            const unsigned short* s = reinterpret_cast<const unsigned short*>(buf.data());
            for (size_t i = 0; i < nsamp; ++i)
                out[i] = (unsigned char)(((unsigned int)s[i] * 255 + 32767) / 65535);
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
