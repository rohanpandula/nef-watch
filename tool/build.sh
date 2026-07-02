#!/usr/bin/env bash
# Build the nef_render helper and stage the SDK runtime resources next to it.
#
# Override the SDK location with: SDK_DIR=/path/to/Image\ SDK/Library/Mac bash build.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${SDK_DIR:-/Users/rohan/Downloads/nx-tiffexport/Image SDK/Library/Mac}"
SAMPLE="$SDK/Sample"
LIBDIR="$SAMPLE/Lib/release"
PROFILE="$SDK/Profiles/NKsRGB.icm"

if [[ ! -f "$LIBDIR/libImgSDK.dylib" ]]; then
  echo "ERROR: SDK not found. Set SDK_DIR to your 'Image SDK/Library/Mac'." >&2
  echo "  looked in: $LIBDIR" >&2
  exit 1
fi

if [[ ! -f "$PROFILE" ]]; then
  echo "ERROR: Nikon sRGB profile not found in the SDK." >&2
  echo "  looked in: $PROFILE" >&2
  exit 1
fi

# Strip Gatekeeper quarantine so dyld loads the unsigned SDK dylibs.
xattr -dr com.apple.quarantine "$SDK" 2>/dev/null || true

# The SDK requires prm.bin at <executable>/Contents/Resources/prm.bin at render time.
mkdir -p "$HERE/Contents/Resources"
cp -f "$SAMPLE/Resources/prm.bin" "$HERE/Contents/Resources/prm.bin"

# Stage the Nikon sRGB profile as nef_watch.py's default --profile.
cp -f "$PROFILE" "$HERE/Contents/Resources/NKsRGB.icm"

COMMON=(-std=c++17 -O2 -Wno-deprecated-declarations -I"$SAMPLE" -I"$SDK/Include" -include "$HERE/prefix.h")

# NkImageLibCtrl.cpp must be plain C++ (keeps Nkfl_Entry's extern "C"); ObjC-bearing
# files are Objective-C++.
clang++ "${COMMON[@]}" -x objective-c++ -c "$HERE/nef_render.mm"        -o "$HERE/nef_render.o"
clang++ "${COMMON[@]}" -x c++           -c "$SAMPLE/NkImageLibCtrl.cpp" -o "$HERE/NkImageLibCtrl.o"
clang++ "${COMMON[@]}" -x objective-c++ -c "$SAMPLE/NkILSampleUtils.cpp" -o "$HERE/NkILSampleUtils.o"

clang++ "$HERE/nef_render.o" "$HERE/NkImageLibCtrl.o" "$HERE/NkILSampleUtils.o" \
  -L"$LIBDIR" -lImgSDK -Wl,-rpath,"$LIBDIR" \
  -framework Cocoa -framework Carbon -framework ApplicationServices \
  -framework CoreServices -framework Foundation \
  -o "$HERE/nef_render"

rm -f "$HERE"/*.o

# rand_freeze.dylib: optional determinism shim for nef_watch.py --deterministic.
# Interposes libc rand()/srand() so the SDK's rand()-seeded dither is reproducible.
clang -dynamiclib -O2 "$HERE/rand_freeze.c" -o "$HERE/rand_freeze.dylib"

echo "built: $HERE/nef_render"
echo "built: $HERE/rand_freeze.dylib"
echo "staged: $HERE/Contents/Resources/prm.bin"
echo "staged: $HERE/Contents/Resources/NKsRGB.icm"
