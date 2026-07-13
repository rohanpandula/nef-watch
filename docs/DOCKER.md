# Linux and Unraid Docker deployment

Nikon does not publish a native Linux Image SDK. `nef-watch` therefore keeps the
Python watcher and TIFF encoder native Linux, while a small Windows console
adapter loads Nikon's unmodified x64 Windows SDK under Wine. Xvfb supplies the
headless display required by the SDK's Windows color-management calls.

This path is `linux/amd64` only and is not an environment Nikon officially
supports. It has been exercised on Unraid 7.3.1 with Nikon Image SDK 1.46. The
SDK remains proprietary: build and retain the image privately, and do not commit
the SDK files or push the resulting image to a public registry.

## Build the private image

The build requires the Windows half of the Nikon Image SDK 1.46.0 download. It
must contain:

```text
Image SDK/Library/win/
├── Include/Nkfl_Interface.h
├── Bin/x64/Release/NkImgSDK.dll
├── Bin/x64/Release/{Elm.dll,Elm.nlf,RCSigProc.dll,tbb.dll,tbbmalloc.dll,prm.bin}
└── Profiles/                         # all 25 files are required
```

BuildKit imports that directory as a separate named context. The SDK is never
copied into the Git worktree or the normal Docker build context:

```bash
cd /path/to/nef-watch
SDK_DIR="/path/to/Image SDK/Library/win" \
  IMAGE="nef-watch:nikon-linux" \
  bash docker/build-image.sh
```

`build-image.sh` verifies every copied runtime/profile and rejects a package
whose combined SHA-256 differs from the six-image v1.46.0 baseline. To test a
new SDK deliberately, set `ALLOW_UNVALIDATED_SDK=1` and establish a new color
baseline before deployment. The fingerprint is also stored on the image as
`io.nef-watch.nikon-sdk.fingerprint`.

For auditable, fail-closed builds, the Dockerfile pins its base-image digests,
direct Debian package versions, and Python wheels by SHA-256. Transitive Debian
packages still resolve from Debian's live repositories, so the resulting image
ID—not the Dockerfile alone—is the immutable runtime identity. The script hashes
the effective Docker/runtime source and writes that value to
`io.nef-watch.source.fingerprint`; use the script rather than a direct
`docker compose build` when recording a validation baseline. The image also
contains `/usr/share/nef-watch/runtime-packages.tsv`, a complete installed
Debian-package inventory. Any dependency or source-fingerprint change requires
rerunning the color corpus before that image can replace a recorded baseline.
The required x64 Visual C++ runtime is fetched from a fixed Microsoft URL with a
pinned SHA-256 and installed into each new Wine volume; its hash is recorded as
`io.nef-watch.vc-redist.sha256`. Keep this third-party runtime in the same
private-image/licensed-use boundary as the Nikon SDK.

For a remote Unraid build, first copy this repository and the private SDK tree to
the server, then run the same command over SSH. If the staging location is under
an exported share, keep the proprietary SDK directory root-only. On the validated
host used for this project, the paths are:

```text
/mnt/user/Media/.nef-watch-build/repo
/mnt/user/Media/.nef-watch-build/nikon-sdk
```

The validated server currently keeps the SDK path at mode `0700` and its files
at mode `0600`. This is not required for rendering; it only controls who can
read the proprietary build input on the host.

## One-shot smoke test

Use absolute Unraid host paths. Input may be mounted read-only; the Python layer
copies EXIF from it but never modifies it. The image defaults to Unraid's
`nobody:users` identity (`99:100`), so create the output with matching ownership
and use a fresh Wine volume (or fix the ownership of an older root-owned one):

```bash
install -d -o 99 -g 100 /mnt/user/Photos/TIFF
docker volume create nef-watch-wine
```

Then run the same confinement used by Compose:

```bash
docker run --rm \
  --platform linux/amd64 \
  --user 99:100 \
  --network none \
  --memory 4g \
  --memory-swap 4g \
  --cpus 4 \
  --pids-limit 256 \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=1g \
  --tmpfs /tmp/.X11-unix:rw,noexec,nosuid,nodev,mode=1777,size=1m \
  --tmpfs /run:rw,noexec,nosuid,nodev,mode=0755,size=8m \
  -v nef-watch-wine:/var/lib/nef-watch \
  -v /mnt/user/Photos/NEF:/input:ro \
  -v /mnt/user/Photos/TIFF:/output \
  nef-watch:nikon-linux \
  /input --out /output --once --recursive --jobs 4
```

The `nef-watch-wine` volume preserves Wine's initialized prefix, avoiding the
first-start setup cost on every container run.

## Continuous watcher

```bash
docker run -d \
  --name nef-watch \
  --restart unless-stopped \
  --platform linux/amd64 \
  --user 99:100 \
  --network none \
  --memory 4g \
  --memory-swap 4g \
  --cpus 4 \
  --pids-limit 256 \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=1g \
  --tmpfs /tmp/.X11-unix:rw,noexec,nosuid,nodev,mode=1777,size=1m \
  --tmpfs /run:rw,noexec,nosuid,nodev,mode=0755,size=8m \
  -v nef-watch-wine:/var/lib/nef-watch \
  -v /mnt/user/Photos/NEF:/input:ro \
  -v /mnt/user/Media/Photos/Nikon-TIFF:/output \
  nef-watch:nikon-linux \
  /input --out /output --recursive --jobs 4 --interval 3
```

Change the two host paths to the folders you actually want watched and written.
Do not point the output inside the input tree. Pre-create the output as UID 99,
GID 100 as shown above. If you intentionally choose another Unraid identity,
rebuild with matching `NEF_WATCH_UID`/`NEF_WATCH_GID` args and pass that same
identity with `--user`.

## Docker Compose

Copy the example environment file, edit its host paths, build through the
provenance-aware wrapper, and then start Compose without rebuilding:

```bash
cp docker/.env.example docker/.env
SDK_DIR="$(sed -n 's/^NIKON_SDK_DIR=//p' docker/.env)" \
  IMAGE="$(sed -n 's/^IMAGE=//p' docker/.env)" \
  PUID="$(sed -n 's/^PUID=//p' docker/.env)" \
  PGID="$(sed -n 's/^PGID=//p' docker/.env)" \
  bash docker/build-image.sh
docker compose --project-directory docker up -d --no-build
docker compose --project-directory docker logs -f nef-watch
```

`docker compose build` remains available for local experimentation, but its
source label is intentionally `compose-direct-unverified`; do not use a direct
Compose build as a recorded validation baseline.

Before `up`, pre-create `NEF_OUTPUT` with the `PUID`/`PGID` from `.env` (defaults
to `99:100`). If an older named Wine volume was initialized as root, recreate it
when no container is using it, or change its contents to the configured owner;
otherwise startup exits with a clear unwritable-prefix error.

The Compose service has no published ports or runtime network access. It also
runs as a non-root user with Linux capabilities dropped, a read-only root
filesystem, and only `/tmp`, the Wine volume, and the output mount writable. The
defaults also cap it at 4 CPUs, 4 GiB of memory, and 256 PIDs. The Compose file
requests a memory-plus-swap ceiling equal to the memory ceiling; on Unraid hosts
without swap-controller support, Docker warns and enforces the memory limit
without separate swap accounting. Override `CPU_LIMIT`, `MEMORY_LIMIT`, or
`PIDS_LIMIT` in `.env` only after measuring a known-valid workload. Docker's
JSON logs rotate at 10 MiB with three files, so a long-running watcher cannot
fill Docker storage with unbounded logs.

Keep the watched input private or authenticated when possible. A writable public
drop folder lets any LAN client feed files to a proprietary parser. The container
is isolated and resource-limited, and `nef-watch` rejects inputs above 512 MiB by
default, but those are containment layers rather than a substitute for trusted
input. Use `--max-input-mib` only for a known-valid larger NEF/NRW.

## Color validation

Validate decoded RGB values and the embedded ICC profile rather than TIFF file
bytes, since lossless compression and metadata layout can differ:

```bash
python3 tool/validate_tiffs.py \
  nx=/validation/nx-studio.tif \
  mac=/validation/mac-sdk.tif \
  linux=/validation/linux-docker.tif
```

The validator is intentionally exact: any changed sample returns exit code 1.
The current NX Studio-versus-native-Mac SDK baseline already fails this strict
gate (MAE about 1.74/255, maximum 7), so Linux cannot make the four-way equality
true without first resolving that pre-existing gap. Two supplied NX Studio
exports of the same NEF also differ from each other (MAE about 2.28/255), which
the controlled SDK matrix localizes to the 8-bit noise/dither realization. See
[VALIDATION.md](VALIDATION.md).

## Current limitations

- `--deterministic` is native-macOS-only. The Docker path deliberately does not
  patch or interpose Nikon's Windows binary.
- The image contains TIFF/JPEG dependencies and ExifTool, but not `dnglab` or
  Adobe DNG Converter. Add a native Linux `dnglab` binary if DNG output is needed.
- Nikon optional Picture Controls must be installed in the Wine prefix if a NEF
  depends on one. Unlike Nikon's sample wrapper, `nef-watch` treats every
  non-zero `OpenSession` warning as fatal: Nikon documents warning fallbacks
  that substitute a different Picture Control or white-balance mode, which
  would violate the exact-color contract. The documented `GetImageData` warning
  that only declines a low-resolution optimization remains safe and accepted.
- Keep the private Nikon SDK build context and resulting image on systems whose
  users are authorized under Nikon's SDK agreement.
