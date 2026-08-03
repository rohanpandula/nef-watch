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
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <fcntl.h>
#include <io.h>
#include <limits>
#include <new>
#include <string>
#include <vector>

namespace {

HMODULE g_sdk = nullptr;
Nkfl_EntryProcPtr g_entry = nullptr;
bool g_libraryOpen = false;
char g_swapPath[MAX_PATH] = {};
constexpr std::uint64_t kMaxRenderedPixels = 100000000ULL;
constexpr std::uint64_t kMaxRenderedBytes = 800000000ULL;
constexpr unsigned long kDefaultRenderMemoryMiB = 1536;
constexpr unsigned long kMinRenderMemoryMiB = 768;
constexpr unsigned long kMaxRenderMemoryMiB = 16384;
constexpr unsigned long kDefaultSdkMemoryMiB = 512;
constexpr unsigned long kMinSdkMemoryMiB = 256;
constexpr unsigned long kRendererOverheadMiB = 256;
unsigned long g_renderMemoryMiB = kDefaultRenderMemoryMiB;
unsigned long g_sdkMemoryMiB = kDefaultSdkMemoryMiB;
unsigned long g_bufferMemoryMiB =
    kDefaultRenderMemoryMiB - kDefaultSdkMemoryMiB - kRendererOverheadMiB;

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
        case kNkfl_Color_RGB_Gray: return 3;
        case kNkfl_Color_RGB: return 3;
        default: return 0;
    }
}

bool parseMemoryEnvironment(const char* name, unsigned long fallback,
                            unsigned long minimum, unsigned long maximum,
                            unsigned long* memoryMiB) {
    const char* value = std::getenv(name);
    if (!value || value[0] == '\0') {
        *memoryMiB = fallback;
        return true;
    }
    for (const char* digit = value; *digit != '\0'; ++digit) {
        if (*digit < '0' || *digit > '9') {
            std::fprintf(stderr, "%s must contain decimal digits only\n", name);
            return false;
        }
    }
    errno = 0;
    char* end = nullptr;
    const unsigned long parsed = std::strtoul(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed < minimum ||
        parsed > maximum) {
        std::fprintf(stderr, "%s must be an integer from %lu to %lu\n", name,
                     minimum, maximum);
        return false;
    }
    *memoryMiB = parsed;
    return true;
}

bool configuredRenderMemory(unsigned long* totalMiB, unsigned long* sdkMiB,
                            unsigned long* bufferMiB) {
    if (!parseMemoryEnvironment(
            "NEF_WATCH_RENDER_MEMORY_MIB", kDefaultRenderMemoryMiB,
            kMinRenderMemoryMiB, kMaxRenderMemoryMiB, totalMiB) ||
        !parseMemoryEnvironment(
            "NEF_WATCH_SDK_MEMORY_MIB", kDefaultSdkMemoryMiB,
            kMinSdkMemoryMiB, kMaxRenderMemoryMiB, sdkMiB)) {
        return false;
    }
    if (*sdkMiB > *totalMiB - kRendererOverheadMiB ||
        *totalMiB - kRendererOverheadMiB - *sdkMiB < 256) {
        std::fprintf(
            stderr,
            "render memory budget is too small: total=%lu MiB sdk=%lu MiB; "
            "at least %lu MiB overhead and 256 MiB pixels are required\n",
            *totalMiB, *sdkMiB, kRendererOverheadMiB);
        return false;
    }
    *bufferMiB = *totalMiB - kRendererOverheadMiB - *sdkMiB;
    return true;
}

bool hasWindowsPrefix(const char* path, const char* prefix) {
    return _strnicmp(path, prefix, std::strlen(prefix)) == 0;
}

bool safeRendererPath(const char* path, const char* requiredPrefix,
                      const char* label) {
    if (!hasWindowsPrefix(path, requiredPrefix)) {
        std::fprintf(stderr, "%s must be beneath %s\n", label, requiredPrefix);
        return false;
    }
    const char* relative = path + std::strlen(requiredPrefix);
    if (*relative == '\0' || std::strstr(relative, "..") != nullptr ||
        std::strchr(relative, ':') != nullptr ||
        std::strchr(relative, '/') != nullptr) {
        std::fprintf(stderr, "%s contains an unsafe Windows path component\n", label);
        return false;
    }
    return true;
}

FILE* openExclusiveOutput(const char* rawPath) {
    HANDLE handle = CreateFileA(
        rawPath, GENERIC_WRITE, 0, nullptr, CREATE_NEW,
        FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_SEQUENTIAL_SCAN |
            FILE_FLAG_OPEN_REPARSE_POINT,
        nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "cannot exclusively create output '%s' [Win32 %lu]\n",
                     rawPath, GetLastError());
        return nullptr;
    }
    BY_HANDLE_FILE_INFORMATION information = {};
    if (!GetFileInformationByHandle(handle, &information) ||
        (information.dwFileAttributes &
         (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
        information.nNumberOfLinks != 1) {
        const DWORD error = GetLastError();
        std::fprintf(stderr,
                     "new output is not a single-link regular file [Win32 %lu]\n",
                     error);
        CloseHandle(handle);
        DeleteFileA(rawPath);
        return nullptr;
    }
    const int descriptor = _open_osfhandle(
        reinterpret_cast<intptr_t>(handle), _O_WRONLY | _O_BINARY);
    if (descriptor < 0) {
        std::fprintf(stderr, "cannot attach output handle: %s\n", std::strerror(errno));
        CloseHandle(handle);
        DeleteFileA(rawPath);
        return nullptr;
    }
    FILE* file = _fdopen(descriptor, "wb");
    if (!file) {
        std::fprintf(stderr, "cannot create output stream: %s\n", std::strerror(errno));
        _close(descriptor);
        DeleteFileA(rawPath);
        return nullptr;
    }
    return file;
}

bool parseBits(const char* value, int* bits) {
    if (std::strcmp(value, "8") == 0) {
        *bits = 8;
        return true;
    }
    if (std::strcmp(value, "16") == 0) {
        *bits = 16;
        return true;
    }
    std::fprintf(stderr, "bits must be exactly 8 or 16\n");
    return false;
}

bool parseExposure(const char* value, double* exposure) {
    errno = 0;
    char* end = nullptr;
    const double parsed = std::strtod(value, &end);
    if (errno != 0 || end == value || *end != '\0' || !std::isfinite(parsed) ||
        parsed < -5.0 || parsed > 5.0) {
        std::fprintf(stderr,
                     "expcomp_ev must be a finite number from -5 through 5\n");
        return false;
    }
    *exposure = parsed;
    return true;
}

bool loadSdk() {
    wchar_t executablePath[32768] = {};
    const DWORD executableLength = GetModuleFileNameW(
        nullptr, executablePath,
        static_cast<DWORD>(sizeof(executablePath) / sizeof(executablePath[0])));
    if (executableLength == 0 ||
        executableLength >= sizeof(executablePath) / sizeof(executablePath[0])) {
        std::fprintf(stderr, "GetModuleFileNameW failed or returned a truncated path "
                             "[Win32 %lu]\n",
                     GetLastError());
        return false;
    }
    wchar_t* separator = std::wcsrchr(executablePath, L'\\');
    wchar_t* slash = std::wcsrchr(executablePath, L'/');
    if (!separator || (slash && slash > separator)) separator = slash;
    if (!separator) {
        std::fprintf(stderr, "renderer executable path has no directory\n");
        return false;
    }
    constexpr wchar_t kSdkName[] = L"NkImgSDK.dll";
    const std::size_t directoryLength =
        static_cast<std::size_t>(separator - executablePath + 1);
    if (directoryLength + (sizeof(kSdkName) / sizeof(kSdkName[0])) >
        sizeof(executablePath) / sizeof(executablePath[0])) {
        std::fprintf(stderr, "renderer executable directory is too long\n");
        return false;
    }
    std::wmemcpy(executablePath + directoryLength, kSdkName,
                 sizeof(kSdkName) / sizeof(kSdkName[0]));

    // Remove the working directory from both explicit and transitive DLL
    // searches. The Nikon DLL and its private dependencies must come from the
    // immutable, attested directory beside this executable; MSVC dependencies
    // come from System32 in the private Wine prefix.
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_APPLICATION_DIR |
                                  LOAD_LIBRARY_SEARCH_SYSTEM32)) {
        std::fprintf(stderr, "SetDefaultDllDirectories failed [Win32 %lu]\n",
                     GetLastError());
        return false;
    }
    g_sdk = LoadLibraryExW(executablePath, nullptr,
                           LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                               LOAD_LIBRARY_SEARCH_APPLICATION_DIR |
                               LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!g_sdk) {
        std::fprintf(stderr, "LoadLibraryExW(<renderer-dir>/NkImgSDK.dll) failed "
                             "[Win32 %lu]\n",
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

    if (!configuredRenderMemory(&g_renderMemoryMiB, &g_sdkMemoryMiB,
                                &g_bufferMemoryMiB)) {
        return false;
    }
    param.ulVMMemorySize = g_sdkMemoryMiB;

    const char* configuredTemp = std::getenv("NEF_WATCH_WINE_TEMP_DIR");
    if (!configuredTemp || configuredTemp[0] == '\0') {
        std::fprintf(stderr,
                     "NEF_WATCH_WINE_TEMP_DIR is required; refusing to place "
                     "Nikon swap data in Wine's persistent state\n");
        return false;
    }
    const std::size_t configuredTempLength = std::strlen(configuredTemp);
    if (configuredTempLength < 3 || configuredTempLength >= MAX_PATH - 1 ||
        ((configuredTemp[0] < 'A' || configuredTemp[0] > 'Z') &&
         (configuredTemp[0] < 'a' || configuredTemp[0] > 'z')) ||
        configuredTemp[1] != ':' ||
        (configuredTemp[2] != '\\' && configuredTemp[2] != '/')) {
        std::fprintf(stderr,
                     "NEF_WATCH_WINE_TEMP_DIR must be an absolute Windows "
                     "drive path shorter than MAX_PATH\n");
        return false;
    }
    char tempDir[MAX_PATH] = {};
    std::memcpy(tempDir, configuredTemp, configuredTempLength + 1);
    std::size_t tempLength = configuredTempLength;
    if (tempDir[tempLength - 1] != '\\' && tempDir[tempLength - 1] != '/') {
        tempDir[tempLength++] = '\\';
        tempDir[tempLength] = '\0';
    }
    const DWORD tempAttributes = GetFileAttributesA(tempDir);
    if (tempAttributes == INVALID_FILE_ATTRIBUTES ||
        !(tempAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
        (tempAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
        std::fprintf(stderr,
                     "configured Nikon temporary path is not a real directory "
                     "[Win32 %lu]\n",
                     GetLastError());
        return false;
    }
    const char* swapPath = std::getenv("NEF_WATCH_WINE_SWAP_PATH");
    if (!swapPath || swapPath[0] == '\0') {
        std::fprintf(stderr,
                     "NEF_WATCH_WINE_SWAP_PATH is required; the Linux wrapper "
                     "must own the unique swap-file lifecycle\n");
        return false;
    }
    const std::size_t swapLength = std::strlen(swapPath);
    if (swapLength <= tempLength || swapLength >= sizeof(g_swapPath) ||
        std::strncmp(swapPath, tempDir, tempLength) != 0 ||
        std::strchr(swapPath + tempLength, '\\') != nullptr ||
        std::strchr(swapPath + tempLength, '/') != nullptr) {
        std::fprintf(stderr,
                     "NEF_WATCH_WINE_SWAP_PATH must name one direct child of "
                     "NEF_WATCH_WINE_TEMP_DIR\n");
        return false;
    }
    const DWORD swapAttributes = GetFileAttributesA(swapPath);
    if (swapAttributes == INVALID_FILE_ATTRIBUTES ||
        (swapAttributes &
         (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
        std::fprintf(stderr,
                     "configured Nikon swap path is not a real file [Win32 %lu]\n",
                     GetLastError());
        return false;
    }
    std::memcpy(g_swapPath, swapPath, swapLength + 1);
    const std::size_t profilePathLength = std::strlen(tempDir);
    if (swapLength >= sizeof(param.VMFileInfo) ||
        profilePathLength >= sizeof(param.DefProfPath)) {
        std::fprintf(stderr, "Wine temporary path does not fit Nikon SDK fields\n");
        return false;
    }
    std::memcpy(param.VMFileInfo, swapPath, swapLength + 1);
    std::memcpy(param.DefProfPath, tempDir, profilePathLength + 1);

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
    if (g_swapPath[0] != '\0') {
        if (!DeleteFileA(g_swapPath) && GetLastError() != ERROR_FILE_NOT_FOUND) {
            std::fprintf(stderr, "cannot remove Nikon swap file [Win32 %lu]\n",
                         GetLastError());
        }
        g_swapPath[0] = '\0';
    }
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
    if (argc < 4 || argc > 6) {
        std::fprintf(stderr,
                     "usage: %s <input.nef> <output.raw> <profile.icm> "
                     "[bits=8] [expcomp_ev=0]\n",
                     argv[0]);
        return 1;
    }

    const char* nefPath = argv[1];
    const char* rawPath = argv[2];
    const char* profilePath = argv[3];
    int outBits = 8;
    double expComp = 0.0;
    if ((argc >= 5 && !parseBits(argv[4], &outBits)) ||
        (argc >= 6 && !parseExposure(argv[5], &expComp))) {
        return 1;
    }
    if (!safeRendererPath(nefPath, "T:\\input\\", "input") ||
        !safeRendererPath(rawPath, "T:\\output\\", "output") ||
        !safeRendererPath(profilePath, "R:\\Profiles\\", "profile")) {
        return 1;
    }

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
        std::fprintf(stderr,
                     "unsupported SDK image format (NKRAW1 requires RGB): "
                     "color=0x%lx depth=%lu\n",
                     info.ulColor, info.ulByteDepth);
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }
    if (outBits == 16 && info.ulByteDepth != 2) {
        std::fprintf(stderr,
                     "16-bit output requested but Nikon SDK returned only "
                     "%lu-bit samples\n",
                     info.ulByteDepth * 8);
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
    const std::uint64_t configuredBufferLimit =
        static_cast<std::uint64_t>(g_bufferMemoryMiB) * 1024ULL * 1024ULL;
    if (sourceBytes64 > kMaxRenderedBytes ||
        sourceBytes64 > configuredBufferLimit ||
        !checkedSize(sourceBytes64, &sourceBytes)) {
        std::fprintf(stderr,
                     "rendered image needs %llu bytes; configured per-render "
                     "limit is %llu bytes (absolute limit %llu)\n",
                     static_cast<unsigned long long>(sourceBytes64),
                     static_cast<unsigned long long>(configuredBufferLimit),
                     static_cast<unsigned long long>(kMaxRenderedBytes));
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }

    std::vector<unsigned char> source;
    try {
        source.resize(sourceBytes);
    } catch (const std::bad_alloc&) {
        std::fprintf(stderr, "cannot allocate %zu-byte rendered image buffer\n",
                     sourceBytes);
        closeSession(sessionId);
        closeLibrary();
        return 5;
    }
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
    if (info.ulByteDepth == 2 && outBits == 8) {
        // Convert forward in-place: each output byte is written below the two
        // source bytes needed by all current and future iterations. This avoids
        // a second hundreds-of-megabytes allocation under the cgroup limit.
        for (std::size_t i = 0; i < samples; ++i) {
            std::uint16_t value = 0;
            std::memcpy(&value, source.data() + i * sizeof(value), sizeof(value));
            source[i] = static_cast<unsigned char>(
                (static_cast<std::uint32_t>(value) * 255u + 32767u) / 65535u);
        }
        outputDepth = 1;
    }

    FILE* file = openExclusiveOutput(rawPath);
    if (!file) {
        return 7;
    }
    std::fprintf(file, "NKRAW1 %lu %lu %d %lu %lu\n", info.ulWidth,
                 info.ulHeight, channels, outputDepth, info.ulOrientation);
    const std::size_t outputBytes = samples * outputDepth;
    const std::size_t written = std::fwrite(source.data(), 1, outputBytes, file);
    const int closeResult = std::fclose(file);
    if (written != outputBytes || closeResult != 0) {
        std::fprintf(stderr, "short write %zu/%zu\n", written, outputBytes);
        return 7;
    }

    std::printf("OK %lux%lu %dch %lubit orient=%lu\n", info.ulWidth,
                info.ulHeight, channels, outputDepth * 8, info.ulOrientation);
    return 0;
}
