# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Public, SDK-free image. Nikon's x86-64 Windows SDK is supplied as a read-only
# runtime mount and copied into private persistent state only after validation.
FROM --platform=linux/amd64 python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    NEF_WATCH_APP_HOME=/var/lib/nef-watch/home \
    NEF_WATCH_STATE_DIR=/var/lib/nef-watch \
    NEF_WATCH_APP_STATE_DIR=/var/lib/nef-watch/app-state \
    NEF_WATCH_UID=99 \
    NEF_WATCH_GID=100 \
    NEF_WATCH_WINE_SCHEMA=wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3 \
    NEF_WATCH_WINE_TEMPLATE=/var/lib/nef-watch/wine-template-wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3 \
    NEF_WATCH_REQUIRE_LANDLOCK=1 \
    NEF_WATCH_RENDER_JOBS=1 \
    NIKON_SDK_DIR=/nikon-sdk \
    NIKON_RUNTIME_DIR=/var/lib/nef-watch/nikon-runtime/current \
    WINEARCH=win64 \
    WINEDEBUG=-all \
    XDG_RUNTIME_DIR=/tmp/nef-watch-runtime \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Unraid's default share owner is nobody:users (99:100). The privileged
# initializer uses a root-only home, then the entrypoint supplies the separate
# app home while irrevocably dropping to this fixed uid/gid.
ARG NEF_WATCH_UID=99
ARG NEF_WATCH_GID=100
ARG NEF_WATCH_SOURCE_FINGERPRINT=unknown
ARG NEF_WATCH_SOURCE_REVISION=unknown

# Nikon's Windows DLL is linked against MSVC 14. Keep an immutable
# Microsoft-hosted redistributable snapshot in the public image; entrypoint.sh
# installs it once into a sealed template copied to disposable per-render prefixes.
ADD --checksum=sha256:cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b \
    https://download.visualstudio.microsoft.com/download/pr/65f6a2c9-1b34-43ef-8a5d-4bbdd407197a/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe \
    /opt/microsoft/VC_redist.x64.exe

# Use the authenticated Debian snapshot that the pinned base image records in
# its debian.sources comments.  Exact package versions remain rebuildable after
# moving Bookworm mirrors prune superseded binaries.  Signature verification
# stays enabled; only the archived Release file's elapsed Valid-Until is ignored.
RUN sed -ri \
      's|^URIs: http://deb.debian.org/debian$|URIs: https://snapshot.debian.org/archive/debian/20260802T000000Z|; s|^URIs: http://deb.debian.org/debian-security$|URIs: https://snapshot.debian.org/archive/debian-security/20260802T000000Z|' \
      /etc/apt/sources.list.d/debian.sources \
    && grep -Fx 'URIs: https://snapshot.debian.org/archive/debian/20260802T000000Z' \
         /etc/apt/sources.list.d/debian.sources \
    && grep -Fx 'URIs: https://snapshot.debian.org/archive/debian-security/20260802T000000Z' \
         /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
         g++-mingw-w64-x86-64=12.2.0-14+25.2 \
         libimage-exiftool-perl=12.57+dfsg-1 \
         libcurl4=7.88.1-10+deb12u15 \
         libgl1-mesa-dri=22.3.6-1+deb12u2 \
         libglapi-mesa=22.3.6-1+deb12u2 \
         libglx-mesa0=22.3.6-1+deb12u2 \
         libxfont2=1:2.0.6-1+deb12u1 \
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

COPY tool/nef_watch.py \
     tool/nef_render_wine.sh \
     tool/nef_wine_sandbox.sh \
     tool/validate_tiffs.py \
     tool/verify_runtime_acceptance.py \
     ./tool/
COPY tool/nef_render_win.cpp /usr/src/nef-watch/nef_render_win.cpp
COPY docker/nikon-sdk-v1.46.sha256 /usr/share/nef-watch/nikon-sdk-v1.46.sha256
COPY validation/baseline-v1.json /usr/share/nef-watch/validation-baseline-v1.json
COPY docker/bootstrap_sdk.py /usr/local/libexec/nef-watch-bootstrap-sdk.py
COPY docker/check_health_state.py /usr/local/libexec/nef-watch-check-health-state.py
COPY docker/landlock_exec.py /usr/local/libexec/nef-watch-landlock-exec.py
COPY docker/landlock_probe.py /usr/local/libexec/nef-watch-landlock-probe.py
COPY docker/render_supervisor.py /usr/local/libexec/nef-watch-render-supervisor.py
COPY docker/startup_timeout.sh /usr/local/libexec/nef-watch-startup-timeout
COPY docker/validate_acceptance_marker.py /usr/local/libexec/nef-watch-validate-acceptance.py
COPY docker/validate_storage.py /usr/local/libexec/nef-watch-validate-storage.py
COPY docker/validate_wine_template.py /usr/local/libexec/nef-watch-validate-wine-template.py
COPY docker/entrypoint.sh /usr/local/bin/nef-watch-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/nef-watch-healthcheck

RUN set -eux; \
    case "${NEF_WATCH_SOURCE_FINGERPRINT}" in \
      ''|*[!0-9a-f]*) echo "invalid source fingerprint" >&2; exit 1 ;; \
    esac; \
    case "${NEF_WATCH_SOURCE_REVISION}" in \
      ''|*[!0-9a-f]*) echo "invalid source revision" >&2; exit 1 ;; \
    esac; \
    test "${#NEF_WATCH_SOURCE_FINGERPRINT}" -eq 64; \
    test "${#NEF_WATCH_SOURCE_REVISION}" -eq 40; \
    printf '{"schema_version":1,"source_fingerprint":"%s","source_revision":"%s"}\n' \
      "${NEF_WATCH_SOURCE_FINGERPRINT}" "${NEF_WATCH_SOURCE_REVISION}" \
      > /usr/share/nef-watch/source-identity.json; \
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
    chmod 0555 /app/tool/nef_watch.py \
               /app/tool/nef_render_wine.sh \
               /app/tool/nef_wine_sandbox.sh \
               /app/tool/validate_tiffs.py \
               /app/tool/verify_runtime_acceptance.py \
               /usr/local/bin/nef-watch-entrypoint \
               /usr/local/bin/nef-watch-healthcheck \
               /usr/local/libexec/nef-watch-bootstrap-sdk.py \
               /usr/local/libexec/nef-watch-check-health-state.py \
               /usr/local/libexec/nef-watch-landlock-exec.py \
               /usr/local/libexec/nef-watch-landlock-probe.py \
               /usr/local/libexec/nef-watch-render-supervisor.py \
               /usr/local/libexec/nef-watch-validate-acceptance.py \
               /usr/local/libexec/nef-watch-validate-storage.py \
               /usr/local/libexec/nef-watch-validate-wine-template.py \
               /usr/local/libexec/nef-watch-startup-timeout; \
    chmod 0444 /opt/microsoft/VC_redist.x64.exe \
               /usr/src/nef-watch/nef_render_win.cpp \
               /usr/share/nef-watch/nikon-sdk-v1.46.sha256 \
               /usr/share/nef-watch/source-identity.json \
               /usr/share/nef-watch/validation-baseline-v1.json; \
    mkdir -p /tmp/.X11-unix \
             /work/nef-watch \
             /var/lib/nef-watch/app-state \
             /var/lib/nef-watch/home \
             /var/lib/nef-watch; \
    chmod 1777 /tmp/.X11-unix; \
    chown 0:0 /var/lib/nef-watch; \
    chown -R "${NEF_WATCH_UID}:${NEF_WATCH_GID}" \
      /var/lib/nef-watch/app-state /var/lib/nef-watch/home; \
    chmod 0755 /var/lib/nef-watch; \
    chmod 0700 /var/lib/nef-watch/app-state /var/lib/nef-watch/home

LABEL io.nef-watch.nikon-sdk.bundled="false"
LABEL io.nef-watch.nikon-sdk.expected-manifest-fingerprint="0e8aba70b296966c03c407c8dd77ddee5c073924b9fb38f988881c366a1fbc51"
LABEL io.nef-watch.debian.snapshot="20260802T000000Z"
LABEL io.nef-watch.source.fingerprint="${NEF_WATCH_SOURCE_FINGERPRINT}"
LABEL org.opencontainers.image.revision="${NEF_WATCH_SOURCE_REVISION}"
LABEL io.nef-watch.vc-redist.sha256="cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"

ENV TMPDIR=/work/nef-watch

USER root
HEALTHCHECK --interval=30s --timeout=15s --start-period=15m --retries=3 \
  CMD ["/usr/local/bin/nef-watch-healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/nef-watch-entrypoint"]
CMD ["--help"]
