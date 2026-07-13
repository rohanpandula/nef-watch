# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Build with:
# docker build --platform linux/amd64 \
#   --build-context nikon-sdk=/path/to/Image\ SDK/Library/win \
#   -f docker/Dockerfile -t nef-watch:nikon-linux .

# Nikon ships an x86-64 Windows DLL, so every stage is intentionally amd64 even
# when this image is built on an Apple Silicon workstation.
FROM --platform=linux/amd64 debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df AS nikon-sdk-verified

ARG ALLOW_UNVALIDATED_SDK=0
COPY docker/nikon-sdk-v1.46.sha256 /tmp/nikon-sdk-v1.46.sha256
RUN --mount=type=bind,from=nikon-sdk,source=/,target=/mnt/nikon-sdk,ro \
    printf '%s  %s\n' \
      0e8aba70b296966c03c407c8dd77ddee5c073924b9fb38f988881c366a1fbc51 \
      /tmp/nikon-sdk-v1.46.sha256 \
      | sha256sum --check --strict - \
    && if (cd /mnt/nikon-sdk \
           && sha256sum --check --strict /tmp/nikon-sdk-v1.46.sha256); then \
         :; \
       elif [ "$ALLOW_UNVALIDATED_SDK" = "1" ]; then \
         echo "WARNING: building with an unvalidated Nikon SDK context" >&2; \
       else \
         echo "Nikon SDK context does not match the validated v1.46.0 manifest" >&2; \
         exit 2; \
       fi \
    && while read -r _ required; do \
         install -D -m 0444 \
           "/mnt/nikon-sdk/$required" "/opt/nikon-sdk/$required"; \
       done < /tmp/nikon-sdk-v1.46.sha256

FROM --platform=linux/amd64 debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df AS adapter-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         g++-mingw-w64-x86-64=12.2.0-14+25.2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY tool/nef_render_win.cpp ./
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Include/Nkfl_Interface.h /opt/nikon-sdk/Include/Nkfl_Interface.h

RUN x86_64-w64-mingw32-g++ \
      -std=c++17 -O2 -Wall -Wextra -Werror -Wno-attributes \
      -static -static-libgcc -static-libstdc++ \
      -I/opt/nikon-sdk/Include \
      nef_render_win.cpp -o /nef_render.exe

FROM --platform=linux/amd64 python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/var/lib/nef-watch/home \
    NEF_WATCH_WINE_SCHEMA=vc14-cc0ff0eb1dc3 \
    NIKON_RUNTIME_DIR=/opt/nikon \
    TMPDIR=/tmp \
    WINEARCH=win64 \
    WINEPREFIX=/var/lib/nef-watch/wine \
    WINEDEBUG=-all \
    XDG_RUNTIME_DIR=/tmp/nef-watch-runtime \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Unraid's default share owner is nobody:users (99:100).  Compose passes the
# same values as build args and as the runtime user when they are overridden.
ARG NEF_WATCH_UID=99
ARG NEF_WATCH_GID=100
ARG NEF_WATCH_SDK_FINGERPRINT=unverified
ARG NEF_WATCH_SOURCE_FINGERPRINT=unknown

# Nikon's Windows DLL is linked against MSVC 14. Wine's builtin msvcp140 has
# unimplemented exception paths that can fail under concurrent first use. Keep
# an immutable Microsoft-hosted redistributable snapshot in the private image;
# entrypoint.sh installs it into each new persistent Wine prefix.
ADD --checksum=sha256:cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b \
    https://download.visualstudio.microsoft.com/download/pr/65f6a2c9-1b34-43ef-8a5d-4bbdd407197a/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe \
    /opt/microsoft/VC_redist.x64.exe

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         libimage-exiftool-perl=12.57+dfsg-1 \
         tini=0.19.0-1+b3 \
         util-linux=2.38.1-5+deb12u3 \
         wine=8.0~repack-4 \
         wine64=8.0~repack-4 \
         x11-utils=7.7+5 \
         xvfb=2:21.1.7-3+deb12u12 \
    && mkdir -p /usr/share/nef-watch \
    && dpkg-query -W -f='${binary:Package}\t${Version}\n' \
         | LC_ALL=C sort > /usr/share/nef-watch/runtime-packages.tsv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pyproject.toml docker/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY tool/nef_watch.py tool/nef_render_wine.sh ./tool/
COPY docker/entrypoint.sh /usr/local/bin/nef-watch-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/nef-watch-healthcheck

RUN set -eux; \
    if getent passwd "${NEF_WATCH_UID}" >/dev/null; then \
      echo "NEF_WATCH_UID ${NEF_WATCH_UID} is already in use" >&2; \
      exit 1; \
    fi; \
    if ! getent group "${NEF_WATCH_GID}" >/dev/null; then \
      groupadd --gid "${NEF_WATCH_GID}" nef-watch; \
    fi; \
    useradd --no-create-home --no-log-init \
      --uid "${NEF_WATCH_UID}" \
      --gid "${NEF_WATCH_GID}" \
      --home-dir /var/lib/nef-watch/home \
      --shell /usr/sbin/nologin \
      nef-watch; \
    chmod +x /app/tool/nef_watch.py \
             /app/tool/nef_render_wine.sh \
             /usr/local/bin/nef-watch-entrypoint \
             /usr/local/bin/nef-watch-healthcheck; \
    chmod 0444 /opt/microsoft/VC_redist.x64.exe; \
    mkdir -p /opt/nikon \
             /tmp/.X11-unix \
             /var/lib/nef-watch/home \
             /var/lib/nef-watch/wine; \
    chmod 1777 /tmp/.X11-unix; \
    chown -R "${NEF_WATCH_UID}:${NEF_WATCH_GID}" /var/lib/nef-watch; \
    chmod 0750 /var/lib/nef-watch /var/lib/nef-watch/home /var/lib/nef-watch/wine

COPY --from=adapter-build /nef_render.exe /opt/nikon/nef_render.exe
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/NkImgSDK.dll /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/Elm.dll /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/Elm.nlf /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/RCSigProc.dll /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/tbb.dll /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/tbbmalloc.dll /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Bin/x64/Release/prm.bin /opt/nikon/
COPY --from=nikon-sdk-verified /opt/nikon-sdk/Profiles/ /opt/nikon/Profiles/

# Build-context modes are not trusted: a sensibly root-only SDK staging tree is
# commonly 0600/0700.  The runtime copy is immutable and must be readable by the
# non-root parser account inside this private image.
RUN find /opt/nikon -type d -exec chmod 0555 {} + \
    && find /opt/nikon -type f -exec chmod 0444 {} + \
    && chmod 0555 /opt/nikon/nef_render.exe

LABEL io.nef-watch.nikon-sdk.fingerprint="${NEF_WATCH_SDK_FINGERPRINT}"
LABEL io.nef-watch.source.fingerprint="${NEF_WATCH_SOURCE_FINGERPRINT}"
LABEL io.nef-watch.vc-redist.sha256="cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"

VOLUME ["/var/lib/nef-watch"]
USER nef-watch
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["/usr/local/bin/nef-watch-healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/nef-watch-entrypoint"]
CMD ["--help"]
