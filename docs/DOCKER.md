# Linux and Unraid Docker deployment

Nikon does not publish a native Linux Image SDK. `nef-watch` runs a small
Windows adapter against Nikon's unmodified x64 SDK under Wine while the watcher
and TIFF/JPEG encoder remain native Linux. Xvfb provides the headless display
required by the SDK's Windows color-management calls.

The ready-to-run image is published for `linux/amd64`:

```text
ghcr.io/rohanpandula/nef-watch:linux-amd64
```

Users pull this image; they do not build it. The public image contains no Nikon
headers, DLLs, profiles, or runtime data. You must obtain Nikon Image SDK 1.46.0
yourself from <https://sdk.nikonimaging.com/> and use it under Nikon's terms.

## How first start works

Mount `Image SDK/Library/win` at `/nikon-sdk:ro` and a persistent Docker volume
at `/var/lib/nef-watch`. On first start, the container:

1. verifies the pinned manifest and SHA-256 of every required SDK file;
2. confirms that `/nikon-sdk` is a read-only mount;
3. compiles the open-source Windows adapter with MinGW;
4. copies only the allowlisted Nikon runtime and profiles into the private
   persistent volume; and
5. initializes Wine and starts the watcher.

The staged runtime is keyed by the SDK manifest, adapter source, and compiler.
Normal restarts reuse it without compiling again. An image update that changes
the adapter creates a new private runtime automatically. Concurrent first starts
are serialized, and incomplete staging directories are never activated.

The persistent state volume contains Nikon's licensed files after first start.
Do not publish, export, or attach that volume to an image.

## Prepare the Nikon SDK and folders

The mounted directory must have this layout:

```text
Image SDK/Library/win/
├── Include/Nkfl_Interface.h
├── Bin/x64/Release/NkImgSDK.dll
├── Bin/x64/Release/{Elm.dll,Elm.nlf,RCSigProc.dll,tbb.dll,tbbmalloc.dll,prm.bin}
└── Profiles/                         # all validated profile files
```

The container runs as Unraid's standard `nobody:users` identity (`99:100`). The
SDK must be readable and traversable by that identity, and the output directory
must be writable. The SDK may be world-readable if that suits your server:

```bash
chmod -R a+rX /mnt/user/Media/.nef-watch-build/nikon-sdk
install -d -o 99 -g 100 /mnt/user/Media/Photos/Nikon-TIFF
```

Input can remain read-only. The watcher reads NEFs and copies EXIF but never
modifies source files.

## Recommended Compose setup

Clone the repository only to obtain the Compose file and example settings; no
local image build occurs:

```bash
git clone https://github.com/rohanpandula/nef-watch.git
cd nef-watch
cp docker/.env.example docker/.env
```

Edit `docker/.env`:

```dotenv
NIKON_SDK_DIR=/mnt/user/Media/.nef-watch-build/nikon-sdk
NEF_INPUT=/mnt/user/Photos/NEF
NEF_OUTPUT=/mnt/user/Media/Photos/Nikon-TIFF
IMAGE=ghcr.io/rohanpandula/nef-watch:linux-amd64
JOBS=4
INTERVAL=3
MEMORY_LIMIT=4g
CPU_LIMIT=4
PIDS_LIMIT=256
```

Validate, pull, and start it:

```bash
docker compose --project-directory docker config
docker compose --project-directory docker pull
docker compose --project-directory docker up -d
docker compose --project-directory docker logs -f nef-watch
```

The first start takes longer while the adapter and Wine prefix are initialized.
Look for `Nikon SDK runtime ready:` in the log. Subsequent starts use the same
`nef-watch-state` volume.

The Compose service publishes no ports and has no runtime network access. It
runs as UID/GID `99:100`, drops every Linux capability, enables
`no-new-privileges`, uses a read-only root filesystem, rotates logs, and limits
CPU, memory, swap, and PID usage. Only temporary files, the output directory,
and persistent state are writable.

To update:

```bash
docker compose --project-directory docker pull
docker compose --project-directory docker up -d
```

Keep the state volume. Deleting it removes both the staged Nikon runtime and the
initialized Wine prefix, so the next start performs the full bootstrap again.

## One-shot `docker run`

For a batch conversion without Compose:

```bash
docker volume create nef-watch-state

docker run --rm \
  --platform linux/amd64 \
  --user 99:100 \
  --network none \
  --memory 4g \
  --memory-swap 4g \
  --cpus 4 \
  --pids-limit 256 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=1g \
  --tmpfs /tmp/.X11-unix:rw,noexec,nosuid,nodev,mode=1777,size=1m \
  --tmpfs /run:rw,noexec,nosuid,nodev,mode=0755,size=8m \
  -v /mnt/user/Media/.nef-watch-build/nikon-sdk:/nikon-sdk:ro \
  -v nef-watch-state:/var/lib/nef-watch \
  -v /mnt/user/Photos/NEF:/input:ro \
  -v /mnt/user/Media/Photos/Nikon-TIFF:/output:rw \
  ghcr.io/rohanpandula/nef-watch:linux-amd64 \
  /input --out /output --once --recursive --jobs 4
```

The CLI help does not need the SDK or persistent state:

```bash
docker run --rm ghcr.io/rohanpandula/nef-watch:linux-amd64 --help
```

## SDK validation failures

This release accepts the exact Nikon Image SDK 1.46.0 Windows package used for
the recorded color baseline. A missing or changed header, DLL, profile, or
`prm.bin` fails closed before Wine starts. Check that the mounted path is the
`Library/win` directory itself, that it is readable by UID 99, and that the bind
mount includes `:ro`.

A newer Nikon SDK needs a reviewed manifest update and a fresh color baseline;
there is no runtime flag that silently bypasses the pinned hashes.

## Maintainer-only local build

The published image is built without a Nikon SDK context. Maintainers can build
the same SDK-free wrapper locally for testing:

```bash
IMAGE=nef-watch:linux-amd64 bash docker/build-image.sh
```

The build wrapper records a canonical source fingerprint in the image label
`io.nef-watch.source.fingerprint`. The image also records that the Nikon SDK is
not bundled, the expected SDK-manifest fingerprint, and the pinned Visual C++
Redistributable hash. GitHub Actions scans every candidate image layer against
the Nikon manifest before it is allowed to publish to GHCR.

## Color validation

Validate decoded RGB samples and the embedded ICC profile rather than TIFF file
bytes, because lossless compression and metadata layout can differ:

```bash
python3 tool/validate_tiffs.py \
  nx=/validation/nx-studio.tif \
  mac=/validation/mac-sdk.tif \
  linux=/validation/linux-docker.tif
```

The validator is intentionally exact. The Docker runtime has reproduced its
recorded Linux decoded pixels and Nikon sRGB ICC profile exactly. The broader
NX Studio-versus-SDK gate remains red: repeated NX Studio exports themselves
use different fine dither/noise, while the practical colors are visually the
same. See [VALIDATION.md](VALIDATION.md) for the measurements and provenance.

## Current limitations

- Nikon supports neither native Linux nor this Wine environment.
- `--deterministic` is native-macOS-only; Docker does not patch Nikon's Windows
  binary.
- DNG output needs a separate Linux `dnglab` binary, which is not bundled.
- Optional Picture Controls must be installed in the Wine prefix if a NEF
  depends on one. SDK warnings that substitute a different Picture Control or
  white-balance mode are treated as fatal because they violate the color
  contract.
- Keep the SDK mount, state volume, and source NEFs within the access boundary
  allowed by their respective licenses and your privacy requirements.
