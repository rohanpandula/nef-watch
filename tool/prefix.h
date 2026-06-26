// Carbon supplies `Rect` (aliased to RECT by Nkfl_Interface.h); Cocoa under ObjC.
#ifdef __OBJC__
    #import <Cocoa/Cocoa.h>
#endif
#include <Carbon/Carbon.h>

#ifndef MAX_PATH
    #define MAX_PATH PATH_MAX
#endif
