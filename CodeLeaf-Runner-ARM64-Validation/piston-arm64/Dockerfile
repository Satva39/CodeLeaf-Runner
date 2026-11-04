# syntax=docker/dockerfile:1.7
#
# ARM64 build of the official Engineer Man Piston API.
#
# IMPORTANT:
# - Every stage that compiles or installs architecture-specific code is pinned
#   to TARGETPLATFORM, not BUILDPLATFORM.
# - On an x86-64 Windows machine Buildx uses QEMU emulation for these stages.
# - Isolate and the native waitpid module are built for the target architecture.
#
ARG PISTON_REF=de2b365ac759670a3a0d13ea208a0869a92c7e64
ARG ISOLATE_REF=af6db68042c3aa0ded80787fbb78bc0846ea2114
ARG NODE_BASE=node:24-bookworm-slim
ARG BUILD_DEPS_BASE=buildpack-deps:bookworm

FROM --platform=$TARGETPLATFORM ${BUILD_DEPS_BASE} AS isolate-builder
ARG TARGETARCH
ARG ISOLATE_REF

RUN test "$TARGETARCH" = "arm64" \
    && apt-get update \
    && apt-get install -y --no-install-recommends libcap-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/envicutor/isolate.git /src/isolate \
    && git -C /src/isolate checkout --detach "${ISOLATE_REF}" \
    && make -C /src/isolate -j"$(nproc)" install \
    && test -x /usr/local/bin/isolate \
    && readelf -h /usr/local/bin/isolate | grep -q "AArch64" \
    && test -f /usr/local/etc/isolate \
    && test -d /var/local/lib/isolate \
    && echo "PASS: Isolate ARM64 binary, config file, and box directory installed"

FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS piston-source
ARG PISTON_REF
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/engineer-man/piston.git /src/piston \
    && git -C /src/piston checkout --detach "${PISTON_REF}" \
    && git -C /src/piston rev-parse HEAD > /PISTON_SOURCE_COMMIT

FROM --platform=$TARGETPLATFORM ${NODE_BASE} AS piston-runtime
ARG TARGETARCH
ARG PISTON_REF
ARG ISOLATE_REF

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    NODE_ENV=production

RUN test "$TARGETARCH" = "arm64" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates git file \
       libxml2 gnupg tar coreutils util-linux libc6-dev binutils build-essential \
       locales libpcre3-dev libevent-dev libgmp-dev \
       libncurses6 libncurses-dev libedit-dev libseccomp2 \
       rename procps python3 libreadline-dev libblas-dev liblapack-dev \
       libarpack2-dev libfftw3-dev libglpk-dev libqhull-dev libqrupdate-dev \
       libsuitesparse-dev libsundials-dev libpcre2-dev libcap2 libcap-dev \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && useradd --no-create-home --home-dir /nonexistent --shell /bin/bash piston

# Copy target-architecture Isolate outputs, including its setuid helper/config/data.
COPY --from=isolate-builder /usr/local/bin/isolate /usr/local/bin/isolate
COPY --from=isolate-builder /usr/local/bin/isolate-check-environment /usr/local/bin/isolate-check-environment
COPY --from=isolate-builder /usr/local/etc/isolate /usr/local/etc/isolate
COPY --from=isolate-builder /var/local/lib/isolate /var/local/lib/isolate
RUN chmod 4755 /usr/local/bin/isolate \
    && readelf -h /usr/local/bin/isolate | grep -q "AArch64"

COPY --from=piston-source /src/piston/api/package.json /piston_api/package.json
COPY --from=piston-source /src/piston/api/package-lock.json /piston_api/package-lock.json
COPY --from=piston-source /src/piston/api/src /piston_api/src
COPY --from=piston-source /PISTON_SOURCE_COMMIT /usr/share/codeleaf-piston/PISTON_SOURCE_COMMIT

WORKDIR /piston_api

# Piston depends on a native Node addon (waitpid). npm builds it here, on the
# TARGETPLATFORM image, so it cannot silently inherit an x86-64 binary.
RUN npm install --omit=dev --no-audit --no-fund \
    && npm cache clean --force \
    && node -e 'if (process.arch !== "arm64") process.exit(20)' \
    && node -e 'require("waitpid"); console.log("waitpid native module loaded")' \
    && node -e 'console.log("Node architecture:", process.arch)' \
    && chown -R root:root /piston_api \
    && chmod -R a-w /piston_api \
    && chmod 0755 /piston_api/src/docker-entrypoint.sh \
    && mkdir -p /piston \
    && chown piston:piston /piston

LABEL org.opencontainers.image.title="CodeLeaf ARM64 Piston" \
      org.opencontainers.image.description="ARM64-targeted Piston API built from official Engineer Man source" \
      org.opencontainers.image.source="https://github.com/engineer-man/piston" \
      org.opencontainers.image.revision="de2b365ac759670a3a0d13ea208a0869a92c7e64" \
      org.opencontainers.image.licenses="MIT"

VOLUME ["/piston"]
EXPOSE 2000/tcp
CMD ["/piston_api/src/docker-entrypoint.sh"]
