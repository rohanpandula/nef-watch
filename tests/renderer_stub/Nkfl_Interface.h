// Minimal, clean-room declaration stub for syntax-checking nef_render_win.cpp.
// Values/layout are intentionally not an SDK replacement and are never shipped.
#pragma once

#include <windows.h>

using Nkfl_EntryProcPtr = unsigned long (*)(unsigned long, void*);

enum : unsigned long {
    kNkfl_Code_None = 0,
    kNkfl_Code_Warn_LowResolutionNotApplicable = 0x0104,
    kNkfl_Cmd_OpenLibrary = 1,
    kNkfl_Cmd_CloseLibrary,
    kNkfl_Cmd_SetDevelopColorMode,
    kNkfl_Cmd_GetDevelopColorMode,
    kNkfl_Cmd_OpenSession,
    kNkfl_Cmd_CloseSession,
    kNkfl_Cmd_RawDevelopment,
    kNkfl_Cmd_SetColorProcess,
    kNkfl_Cmd_SetOutputProfile_UTF8,
    kNkfl_Cmd_GetImageInfo,
    kNkfl_Cmd_GetImageData,
    kNkfl_DevelopColorMode_AppliedInCamera = 20,
    kNkfl_Source_FileName_UTF8,
    kNkfl_RawDevelopment_RawParameterSet,
    kNkfl_RawParameterSet_AsShot,
    kNkfl_ColorProcess_AppliedInCamera,
    kNkfl_RawDevelopment_ExpComp,
    kNkfl_RenderingIntent_Relative,
    kNkfl_Color_RGB_Gray,
    kNkfl_Color_RGB,
};

struct NkflLibraryParam {
    unsigned long ulSize;
    unsigned long ulVersion;
    unsigned long ulVMMemorySize;
    char VMFileInfo[MAX_PATH];
    char DefProfPath[MAX_PATH];
};

struct NkflDevelopColorMode {
    unsigned long ulSize;
    long lDevelopColorMode;
};

struct NkflSessionParam {
    unsigned long ulSize;
    unsigned long ulSessionID;
    unsigned long ulType;
    void* pFileInfo;
};

struct NkflRawDevelopmentParam {
    unsigned long ulSize;
    unsigned long ulSessionID;
    unsigned long ulRawDevelopment;
    void* pData;
};

struct NkflRawDevelopment_RawParameterSet {
    unsigned long ulSize;
    unsigned long ulParamterSet;
};

struct NkflRawDevelopment_ExpComp {
    unsigned long ulSize;
    double dbExpComp;
};

struct NkflColorProcess {
    unsigned long ulSize;
    unsigned long ulSessionID;
    unsigned long ulColorProcess;
};

struct NkflOutputProfileParam {
    unsigned long ulSize;
    unsigned long ulSessionID;
    unsigned long ulRenderingIntent;
    char OutputProfile[MAX_PATH];
};

struct NkflImageInfoParam {
    unsigned long ulSize;
    unsigned long ulSessionID;
    unsigned long ulColor;
    unsigned long ulByteDepth;
    unsigned long ulWidth;
    unsigned long ulHeight;
    unsigned long ulOrientation;
};

struct NkflImageParam {
    unsigned long ulSize;
    unsigned long ulSessionID;
    RECT rectArea;
    unsigned long ulDataSize;
    void* pData;
};
