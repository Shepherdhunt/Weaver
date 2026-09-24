# syntax=docker/dockerfile:1
#
# Weaver with a pinned toolchain: Ubuntu 24.04's Clang 18 (with its profile runtime, for coverage),
# LLVM 18, GCC 13, binutils, Make, CMake, Ninja, Meson and git.
#
#   docker build -t weaver .                    # Weaver, with GCC's own points-to analysis
#   docker build --target svf -t weaver:svf .   # also SVF (AGPL-3.0-or-later; see README, "License")
#
# Behind a TLS-inspecting proxy, give the build its CA certificate: --secret id=ca,src=/path/to/ca.pem
# Where only HTTPS gets out, fetch Ubuntu packages over HTTPS:      --build-arg APT_HTTPS=1
# Run it with container/weaver-docker. Guide: docs/container.md

ARG UBUNTU=24.04

# ---- the toolchain Weaver drives -------------------------------------------------------------
FROM ubuntu:${UBUNTU} AS toolchain
ARG DEBIAN_FRONTEND=noninteractive
ARG APT_HTTPS=0
# A TLS-inspecting proxy's CA, when the build is given one. Until ca-certificates is installed it is
# the whole trust store (apt over HTTPS then works through the proxy); afterwards it stays trusted in
# the image, so pip and AI providers work through the same proxy at run time. (The secret is mounted
# readable by root only; apt downloads as its own unprivileged user, hence the chmod.)
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then \
      mkdir -p /usr/local/share/ca-certificates /etc/ssl/certs \
      && cp /run/secrets/ca /usr/local/share/ca-certificates/weaver-build-ca.crt \
      && { [ -s /etc/ssl/certs/ca-certificates.crt ] || cp /run/secrets/ca /etc/ssl/certs/ca-certificates.crt; } \
      && chmod 644 /usr/local/share/ca-certificates/weaver-build-ca.crt /etc/ssl/certs/ca-certificates.crt; \
    fi \
 && if [ "$APT_HTTPS" = 1 ]; then \
      sed -i 's|http://\([a-z.]*\)\.ubuntu\.com|https://\1.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources; \
    fi
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 python3-venv \
      clang llvm libclang-rt-18-dev \
      gcc g++ binutils \
      make cmake ninja-build meson pkg-config \
      git \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- Weaver, built from this checkout --------------------------------------------------------
FROM toolchain AS build
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN python3 -m venv /opt/weaver \
 && /opt/weaver/bin/pip install --no-cache-dir /src

FROM toolchain AS runtime
COPY --from=build /opt/weaver /opt/weaver
# Any user id can run it: weaver-docker runs it as the host user, so the files it writes are theirs.
ENV PATH=/opt/weaver/bin:$PATH \
    HOME=/home/weaver \
    WEAVER_CONTAINER=1
RUN mkdir -p /home/weaver/.config \
 && chmod -R 1777 /home/weaver \
 && git config --system --add safe.directory '*'
WORKDIR /work
EXPOSE 61847
ENTRYPOINT ["weaver"]
CMD ["doctor", "--machine"]

# ---- optional: SVF points-to (the [flow] extra), a separate AGPL tool Weaver runs as a process -
FROM runtime AS svf
RUN /opt/weaver/bin/pip install --no-cache-dir 'pysvf>=1.0.0.43'

# ---- the test suite, run inside the image (docker build --target test ... && docker run ...) ---
FROM svf AS test
RUN /opt/weaver/bin/pip install --no-cache-dir 'pytest>=7'
COPY pyproject.toml /src/
COPY tests /src/tests
WORKDIR /src
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
CMD []

# ---- the default image: Weaver without SVF ---------------------------------------------------
FROM runtime AS weaver
