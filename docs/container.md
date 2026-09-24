# Weaver in a container

The container image carries Weaver and a pinned toolchain, so a Linux machine needs only Docker (or
Podman). The image is Ubuntu 24.04 with:

| Tool | Version | Used for |
|---|---|---|
| Clang, with its profile runtime | 18 | reading the AST (natively, or as GCC's secondary frontend), SVF bitcode, coverage of Clang builds |
| LLVM (`llvm-link`, `llvm-cov`) | 18 | linking bitcode per program, reading Clang coverage |
| GCC, with the LTO plugin | 13 | GCC builds, GCC's own points-to (`-flto -fipa-pta`), coverage with gcov |
| binutils | 2.42 | archives and shared-object exports in the link model |
| Make, CMake, Ninja, Meson, git | 4.3, 3.28, 1.11, 1.3, 2.43 | your build, `ratchet --base`, git snapshots |
| Python | 3.12 | Weaver |

SVF (AGPL-3.0-or-later) is an optional, separate target: `weaver:svf`. Without it, GCC's own points-to
analysis is used. See "License" in the [README](../README.md).

## Build the image

From a checkout of Weaver:

```sh
docker build -t weaver .                     # about 2 minutes; 1.4 GB
docker build --target svf -t weaver:svf .    # also SVF; 1.8 GB
docker run --rm weaver                       # runs 'weaver doctor --machine' inside: expect 0 problems
```

Behind a TLS-inspecting proxy, give the build the proxy's CA certificate. If only HTTPS gets out, also
fetch Ubuntu packages over HTTPS:

```sh
docker build -t weaver --network host \
  --build-arg https_proxy=$https_proxy --build-arg APT_HTTPS=1 \
  --secret id=ca,src=/path/to/proxy-ca.pem .
```

The CA stays trusted inside the image, so pip and AI providers work through the same proxy.

## Run it on a project

`container/weaver-docker` runs the image on the project in the current directory. Put it on your
`PATH` and use it wherever the guides say `weaver`:

```sh
cd ~/src/my-project
weaver-docker doctor
weaver-docker refresh --capture
weaver-docker risk --top 20
weaver-docker serve               # then open the printed link on this machine
```

What the wrapper does, and why:

- **It mounts the current directory at the same path inside.** Compile commands, `.weaver/` and
  reports hold absolute paths, so they stay valid inside and outside the container. Run it from
  the project root, or from a directory above it.
- **It runs as your user** (`--user $(id -u):$(id -g)`), so the files Weaver and your build write
  belong to you.
- **It mounts your Weaver config directory** (`~/.config/weaver`), so AI keys stored with
  `weaver ai key` are kept. It passes `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` through when they are
  set.
- **`serve` publishes the web interface on the host's loopback address only**
  (`-p 127.0.0.1:61847:61847`). Inside the container Weaver has to listen on the container's own
  interfaces. The printed sign-in link is for `localhost`, and the token in it is still required.
  The Host header check still rejects other names. Never publish the port on `0.0.0.0`: anyone who
  can reach it and get the link could drive builds on your machine.

Settings:

| Variable | Default | Meaning |
|---|---|---|
| `WEAVER_IMAGE` | `weaver` | the image, e.g. `weaver:svf` |
| `WEAVER_PORT` | `61847` | the web interface's port (the same inside and out) |
| `WEAVER_DOCKER_ARGS` | | extra `docker run` options, e.g. `-v /opt/sdk:/opt/sdk:ro` |

## Your build's own dependencies

The image has a generic C toolchain. A project that needs more (libraries, a code generator, a cross
compiler, an SDK) gets it in one of two ways:

- **Mount it.** `WEAVER_DOCKER_ARGS="-v /opt/sdk:/opt/sdk:ro" weaver-docker refresh --capture`. Use the
  same path as on the host, so the compile commands stay valid.
- **Extend the image:**

  ```dockerfile
  FROM weaver
  RUN apt-get update && apt-get install -y --no-install-recommends libssl-dev zlib1g-dev \
   && rm -rf /var/lib/apt/lists/*
  ```

  Then use `WEAVER_IMAGE=my-weaver weaver-docker …`.

Capture and validation run inside the container, with its compilers. So the analysed build is the
container's build. Build and test inside the container too, so that both use the same toolchain.
`weaver doctor --build` (below) checks that the validation build compiles what was analysed.

## Test the image

The `test` target adds pytest and Weaver's test suite to the SVF image:

```sh
docker build --target test -t weaver:test . && docker run --rm weaver:test
```

## Not yet

- **No published image or signed releases.** Build it from the checkout. Publishing (for example to
  a registry that requires sign-in) is part of the paid beta (see the
  [deployment plan](deployment-plan.md)).
- **Linux x86-64 hosts only.** An arm64 image and Docker Desktop on macOS have not been tried.
