// nef_render_win -- Windows Nikon Image SDK adapter for Linux/Wine.
//
// This is intentionally built inside the Docker image against the header from
// the user's Nikon SDK download.  Nikon's proprietary header and DLLs are not
// part of this repository.
//
// usage: nef_render.exe <input.nef> <output.raw> <profile.icm>
//                       [bits=8] [expcomp_ev=0]
//
// Raw output is the same NKRAW1 stream produced by the native macOS helper.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "Nkfl_Interface.h"

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

namespace {

HMODULE g_sdk = nullptr;
Nkfl_EntryProcPtr g_entry = nullptr;
bool g_libraryOpen = false;
constexpr std::uint64_t kMaxRenderedPixels = 100000000ULL;
constexpr std::uint64_t kMaxRenderedBytes = 800000000ULL;

const char* nkflErrText(unsigned long code) {
    switch (code) {
        case 0x0000: return "success";
        case 0x0001: return "out of memory";
        case 0x0002: return "out of resources";
        case 0x0003: return "unsupported operation or file/camera type";
        case 0x0004: return "invalid parameter or corrupt/truncated file";
        case 0x0005: return "wrong call sequence";
        case 0x0006: return "SDK support file or input file missing";
        case 0x0007: return "SDK version mismatch";
        case 0x0008: return "unexpected SDK error";
        case 0x0009: return "file I/O error";
        case 0x000E: return "invalid tag data";
        case 0x0013: return "no shooting data found in image";
        case 0x0014: return "optional Picture Control is not installed";
        case 0x0101: return "optional Picture Control is not applicable";
        case 0x0102: return "edit state does not exist";
        case 0x0103: return "edit state is not applicable";
        case 0x0104: return "low-resolution rendering is not applicable";
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
        default: return (code & 0x0f00) ? "unrecognized SDK warning" : "SDK error";
    }
}

bool isWarning(unsigned long code) {
    // Nikon's official Windows wrapper uses this mask to distinguish warnings
    // that still establish an OpenSession from fatal errors.
    return (code & 0x0f00UL) != 0;
}

bool check(unsigned long code, const char* operation) {
    if (code == kNkfl_Code_None) return true;
    if (isWarning(code)) {
        std::fprintf(stderr,
                     "%s returned a warning that is not valid for this call; "
                     "treating it as failure: %s [0x%04lx]\n",
                     operation, nkflErrText(code), code);
    } else {
        std::fprintf(stderr, "%s failed: %s [0x%04lx]\n", operation,
                     nkflErrText(code), code);
    }
    return false;
}

bool checkOpenSession(unsigned long code) {
    if (code == kNkfl_Code_None) return true;
    if (isWarning(code)) {
        std::fprintf(stderr,
                     "OpenSession warning rejected by strict color mode: %s "
                     "[0x%04lx]; Nikon may have substituted an in-camera "
                     "setting\n",
                     nkflErrText(code), code);
        return false;
    }
    return check(code, "OpenSession");
}

bool checkImageData(unsigned long code) {
    if (code == kNkfl_Code_None) return true;
    // Nikon's sample explicitly treats this warning as success: the requested
    // low-resolution optimization was skipped and new full image data exists.
    if (code == kNkfl_Code_Warn_LowResolutionNotApplicable) {
        std::fprintf(stderr,
                     "GetImageData warning: %s [0x%04lx]; using returned "
                     "full-resolution data\n",
                     nkflErrText(code), code);
        return true;
    }
    return check(code, "GetImageData");
}

bool checkedSize(std::uint64_t value, std::size_t* out) {
    if (value > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max()) ||
        value > static_cast<std::uint64_t>(std::numeric_limits<unsigned long>::max())) {
        return false;
    }
    *out = static_cast<std::size_t>(value);
    return true;
}

int channelsForColor(unsigned long color) {
    switch (color) {
        case kNkfl_Color_Gray: return 1;
        case kNkfl_Color_RGB_Gray: return 3;
        case kNkfl_Color_RGB: return 3;
        case kNkfl_Color_CMYK: return 4;
        default: return 0;
    }
}

bool loadSdk() {
    g_sdk = LoadLibraryExA("NkImgSDK.dll", nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!g_sdk) {
        std::fprintf(stderr, "LoadLibraryExA(NkImgSDK.dll) failed [Win32 %lu]\n",
                     GetLastError());
        return false;
    }
    FARPROC entryAddress = GetProcAddress(g_sdk, "Nkfl_Entry");
    if (!entryAddress) {
        std::fprintf(stderr, "GetProcAddress(Nkfl_Entry) failed [Win32 %lu]\n",
                     GetLastError());
        FreeLibrary(g_sdk);
        g_sdk = nullptr;
        return false;
    }
    static_assert(sizeof(g_entry) == sizeof(entryAddress),
                  "Win32 function pointer sizes must match");
    std::memcpy(&g_entry, &entryAddress, sizeof(g_entry));
    return true;
}

bool openLibrary() {
    if (!loadSdk()) return false;

    NkflLibraryParam param = {};
    param.ulSize = sizeof(param);
    param.ulVersion = 0x01000000;

    MEMORYSTATUSEX memory = {};
    memory.dwLength = sizeof(memory);
    if (GlobalMemoryStatusEx(&memory)) {
        const std::uint64_t mib = memory.ullAvailPhys >> 20;
        param.ulVMMemorySize = static_cast<unsigned long>(
            std::max<std::uint64_t>(256, std::min<std::uint64_t>(mib / 2, 16384)));
    } else {
        param.ulVMMemorySize = 1024;
    }

    char tempDir[MAX_PATH] = {};
    if (GetTempPathA(MAX_PATH, tempDir) == 0) {
        std::strncpy(tempDir, "C:\\windows\\temp\\", MAX_PATH - 1);
    }
    char swapPath[MAX_PATH] = {};
    if (GetTempFileNameA(tempDir, "nkr", 0, swapPath) == 0) {
        std::snprintf(swapPath, sizeof(swapPath), "%snef-watch.swap", tempDir);
    }
    const std::size_t swapLength =
        std::min(std::strlen(swapPath), sizeof(param.VMFileInfo) - 1);
    std::memcpy(param.VMFileInfo, swapPath, swapLength);
    param.VMFileInfo[swapLength] = '\0';
    const std::size_t tempLength =
        std::min(std::strlen(tempDir), sizeof(param.DefProfPath) - 1);
    std::memcpy(param.DefProfPath, tempDir, tempLength);
    param.DefProfPath[tempLength] = '\0';

    if (!check(g_entry(kNkfl_Cmd_OpenLibrary, &param), "OpenLibrary")) return false;
    g_libraryOpen = true;

    // Nikon's v1.46 sample sets this process-global mode immediately after
    // OpenLibrary.  It is distinct from the per-session SetColorProcess call.
    NkflDevelopColorMode requested = {};
    requested.ulSize = sizeof(requested);
    requested.lDevelopColorMode = kNkfl_DevelopColorMode_AppliedInCamera;
    if (!check(g_entry(kNkfl_Cmd_SetDevelopColorMode, &requested),
               "SetDevelopColorMode")) {
        return false;
    }

    NkflDevelopColorMode actual = {};
    actual.ulSize = sizeof(actual);
    if (!check(g_entry(kNkfl_Cmd_GetDevelopColorMode, &actual),
               "GetDevelopColorMode")) {
        return false;
    }
    if (actual.lDevelopColorMode != requested.lDevelopColorMode) {
        std::fprintf(stderr, "GetDevelopColorMode returned %ld, expected %ld\n",
                     actual.lDevelopColorMode, requested.lDevelopColorMode);
        return false;
    }
    return true;
}

void closeLibrary() {
    if (g_entry && g_libraryOpen) g_entry(kNkfl_Cmd_CloseLibrary, nullptr);
    g_libraryOpen = false;
    g_entry = nullptr;
    if (g_sdk) FreeLibrary(g_sdk);
    g_sdk = nullptr;
}

bool closeSession(unsigned long sessionId) {
    if (!sessionId) return true;
    NkflSessionParam param = {};
    param.ulSize = sizeof(param);
    param.ulSessionID = sessionId;
    return check(g_entry(kNkfl_Cmd_CloseSession, &param), "CloseSession");
}

bool rawDevelopment(unsigned long sessionId, unsigned long operation, void* data,
                    const char* label) {
    NkflRawDevelopmentParam param = {};
    param.ulSize = sizeof(param);
    param.ulSessionID = sessionId;
    param.ulRawDevelopment = operation;
    param.pData = data;
    return check(g_entry(kNkfl_Cmd_RawDevelopment, &param), label);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr,
                     "usage: %s <input.nef> <output.raw> <profile.icm> "
                     "[bits=8] [expcomp_ev=0]\n",
                     argv[0]);
        return 1;
    }

    const char* nefPath = argv[1];
    const char* rawPath = argv[2];
    const char* profilePath = argv[3];
    int outBits = argc >= 5 ? std::atoi(argv[4]) : 8;
    const double expComp = argc >= 6 ? std::atof(argv[5]) : 0.0;
    if (outBits != 8 && outBits != 16) outBits = 8;

    if (!openLibrary()) {
        closeLibrary();
        return 2;
    }

    unsigned long sessionId = 0;
    NkflSessionParam session = {};
    session.ulSize = sizeof(session);
    session.ulType = kNkfl_Source_FileName_UTF8;
    session.pFileInfo = const_cast<char*>(nefPath);
    unsigned long err = g_entry(kNkfl_Cmd_OpenSession, &session);
    if (!checkOpenSession(err)) {
        // Warning-class returns can still establish a valid session. Close it
        // before failing rather than accepting Nikon's substituted settings.
        if (session.ulSessionID != 0) closeSession(session.ulSessionID);
        closeLibrary();
        return 3;
    }
    sessionId = session.ulSessionID;
    if (sessionId == 0) {
        std::fprintf(stderr, "OpenSession returned no session ID\n");
        closeLibrary();
        return 3;
    }

    // RawParameterSet=AsShot resets session state and therefore must be first.
    NkflRawDevelopment_RawParameterSet asShot = {};
    asShot.ulSize = sizeof(asShot);
    asShot.ulParamterSet = kNkfl_RawParameterSet_AsShot;
    if (!rawDevelopment(sessionId, kNkfl_RawDevelopment_RawParameterSet,
                        &asShot, "RawParameterSet(AsShot)")) {
        closeSession(sessionId);
        closeLibrary();
        return 4;
    }

    NkflColorProcess color = {};
    color.ulSize = sizeof(color);
    color.ulSessionID = sessionId;
    color.ulColorProcess = kNkfl_ColorProcess_AppliedInCamera;
    if (!check(g_entry(kNkfl_Cmd_SetColorProcess, &color),
               "SetColorProcess(AppliedInCamera)")) {
        closeSession(sessionId);
        closeLibrary();
        return 4;
    }

    if (expComp != 0.0) {
        NkflRawDevelopment_ExpComp exposure = {};
        exposure.ulSize = sizeof(exposure);
        exposure.dbExpComp = expComp;
        if (!rawDevelopment(sessionId, kNkfl_RawDevelopment_ExpComp,
                            &exposure, "SetExpComp")) {
            closeSession(sessionId);
            closeLibrary();
            return 4;
        }
    }

    // The output profile must be last: AsShot silently resets it.
    NkflOutputProfileParam profile = {};
    profile.ulSize = sizeof(profile);
    profile.ulSessionID = sessionId;
    profile.ulRenderingIntent = kNkfl_RenderingIntent_Relative;
    if (std::strlen(profilePath) >= sizeof(profile.OutputProfile)) {
        std::fprintf(stderr, "profile path is too long\n");
        closeSession(sessionId);
        closeLibrary();
        return 4;
    }
    std::memcpy(profile.OutputProfile, profilePath, std::strlen(profilePath) + 1);
    if (!check(g_entry(kNkfl_Cmd_SetOutputProfile_UTF8, &profile),
               "SetOutputProfile")) {
        closeSession(sessionId);
        closeLibrary();
        return 4;
    }

    NkflImageInfoParam info = {};
    info.ulSize = sizeof(info);
    info.ulSessionID = sessionId;
    if (!check(g_entry(kNkfl_Cmd_GetImageInfo, &info), "GetImageInfo")) {
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }

    const int channels = channelsForColor(info.ulColor);
    if (channels == 0 || (info.ulByteDepth != 1 && info.ulByteDepth != 2)) {
        std::fprintf(stderr, "unsupported SDK image format: color=0x%lx depth=%lu\n",
                     info.ulColor, info.ulByteDepth);
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }

    const std::uint64_t renderedPixels =
        static_cast<std::uint64_t>(info.ulWidth) * info.ulHeight;
    if (info.ulWidth == 0 || info.ulHeight == 0 ||
        renderedPixels > kMaxRenderedPixels) {
        std::fprintf(stderr, "unsafe SDK image dimensions: %lux%lu\n",
                     info.ulWidth, info.ulHeight);
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }

    std::size_t sourceBytes = 0;
    const std::uint64_t sourceBytes64 =
        renderedPixels * static_cast<std::uint64_t>(channels) * info.ulByteDepth;
    if (sourceBytes64 > kMaxRenderedBytes ||
        !checkedSize(sourceBytes64, &sourceBytes)) {
        std::fprintf(stderr,
                     "rendered image exceeds the %llu-byte safety limit\n",
                     static_cast<unsigned long long>(kMaxRenderedBytes));
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }

    std::vector<unsigned char> source(sourceBytes);
    NkflImageParam image = {};
    image.ulSize = sizeof(image);
    image.ulSessionID = sessionId;
    image.rectArea.left = 0;
    image.rectArea.top = 0;
    image.rectArea.right = static_cast<LONG>(info.ulWidth);
    image.rectArea.bottom = static_cast<LONG>(info.ulHeight);
    image.ulDataSize = static_cast<unsigned long>(sourceBytes);
    image.pData = source.data();
    if (!checkImageData(g_entry(kNkfl_Cmd_GetImageData, &image))) {
        closeSession(sessionId);
        closeLibrary();
        return 6;
    }

    if (!closeSession(sessionId)) {
        closeLibrary();
        return 6;
    }
    sessionId = 0;
    closeLibrary();

    const std::size_t samples = sourceBytes / info.ulByteDepth;
    unsigned long outputDepth = info.ulByteDepth;
    std::vector<unsigned char> output;
    if (info.ulByteDepth == 2 && outBits == 8) {
        output.resize(samples);
        for (std::size_t i = 0; i < samples; ++i) {
            std::uint16_t value = 0;
            std::memcpy(&value, source.data() + i * sizeof(value), sizeof(value));
            output[i] = static_cast<unsigned char>(
                (static_cast<std::uint32_t>(value) * 255u + 32767u) / 65535u);
        }
        outputDepth = 1;
    } else {
        output.swap(source);
    }

    FILE* file = std::fopen(rawPath, "wb");
    if (!file) {
        std::fprintf(stderr, "cannot open output '%s': %s\n", rawPath,
                     std::strerror(errno));
        return 7;
    }
    std::fprintf(file, "NKRAW1 %lu %lu %d %lu %lu\n", info.ulWidth,
                 info.ulHeight, channels, outputDepth, info.ulOrientation);
    const std::size_t outputBytes = samples * outputDepth;
    const std::size_t written = std::fwrite(output.data(), 1, outputBytes, file);
    const int closeResult = std::fclose(file);
    if (written != outputBytes || closeResult != 0) {
        std::fprintf(stderr, "short write %zu/%zu\n", written, outputBytes);
        return 7;
    }

    std::printf("OK %lux%lu %dch %lubit orient=%lu\n", info.ulWidth,
                info.ulHeight, channels, outputDepth * 8, info.ulOrientation);
    return 0;
}
