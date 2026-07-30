# syntax=docker/dockerfile:1.7

############ Stage 1: builder ############
FROM --platform=$TARGETPLATFORM debian:bookworm AS builder

ARG UPSTREAM_REPO
ARG UPSTREAM_TAG
ARG FG_LABS_REPO
ARG FG_LABS_SHA
# fg-labs/bwa-mem3 BASELINE_ARCH (PR #84): non-kernel TU compile floor on x86.
# Empty string = use the bench-fleet floor of `avx2` (matches upstream's own
# default per fg-labs/bwa-mem3's installation docs and PR #95's multi-arch
# deployment guide). Set to `avx512bw` for an AVX-512BW-host-locked image that
# recovers a few % on hot non-kernel paths on c7a / c7i / m7i. NEON build is
# unaffected; the make variable is silently ignored on aarch64.
#
# Subtlety: `docker buildx` exports `ARG BASELINE_ARCH=""` to the RUN shell as
# the env var `BASELINE_ARCH=` (set-but-empty), and GNU make's `?=` only fires
# when a variable is *unset*, not when it's set-to-empty. Passing the empty
# value through to `make` would leave the Makefile's `BASELINE_ARCH ?= avx2`
# default dormant and silently fall through every `ifeq ($(arch),…)` branch,
# producing a non-kernel-TU build at `sse41` (Makefile's file-level fallback)
# instead of `avx2` — a 10–15 % wall-time tax on every AVX2-capable host per
# fg-labs/bwa-mem3 PR #95's multi-arch deployment doc. The build line below
# uses `${BASELINE_ARCH:-avx2}` so make always sees an explicit non-empty
# value.
ARG BASELINE_ARCH=""
# fg-labs/bwa-mem3 Makefile target. Empty = default (`make` → `bwa-mem3`).
# Supported alternatives:
#   lto-build  → `make lto-build` (Link-Time Optimization). Output is
#                `bwa-mem3.lto`; renamed to the canonical install path
#                below. Roughly doubles build time. See
#                fg-labs/bwa-mem3 docs/src/developer-guide/building.md.
# Used to A/B build-flag perf experiments (LTO, PGO, etc.) against the
# same fg-labs SHA without re-tagging branches in upstream.
ARG FG_LABS_MAKE_TARGET=""
# minibwa is private (lh3); we vendor it as a git submodule at vendor/minibwa
# instead of cloning at build time. MINIBWA_SHA is recorded for traceability
# (image label / docs) but is not used by the build itself — the source pin
# is whatever the submodule commit points at.
ARG MINIBWA_SHA
ARG SAMTOOLS_VERSION
ARG BWA_VERSION
ARG BWAMETH_VERSION
ARG TARGETARCH

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential git cmake curl wget gnupg ca-certificates pkg-config \
        libz-dev libbz2-dev liblzma-dev libcurl4-openssl-dev libssl-dev \
        libncurses-dev libgit2-dev \
        python3 python3-pip python3-venv \
        autoconf automake autoconf-archive libtool \
    && rm -rf /var/lib/apt/lists/*

# clang-19 toolchain (from apt.llvm.org — bookworm ships only clang-14). We
# build all three aligners (bwa-mem2, bwa-mem3, minibwa) with clang rather than
# gcc: clang produces ~16% faster bwa-mem3, ~10% faster bwa-mem2, and ~4%
# faster minibwa on this workload (see fg-labs/bwa-mem3-bench#31 and the
# bwa-mem3 build docs). Kept on the stable bookworm base so the snakemake /
# coordinator Python stack is unchanged; only the compiler moves.
ARG LLVM_VERSION=19
RUN wget -qO /etc/apt/trusted.gpg.d/apt.llvm.org.asc https://apt.llvm.org/llvm-snapshot.gpg.key && \
    echo "deb http://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-${LLVM_VERSION} main" \
        > /etc/apt/sources.list.d/llvm.list && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        clang-${LLVM_VERSION} libomp-${LLVM_VERSION}-dev && \
    rm -rf /var/lib/apt/lists/*
# `clang` / `clang++` wrappers on /usr/local/bin (ahead of /usr/bin on PATH).
# They pin clang-19 and append codegen-neutral compat flags so the 2021-era
# upstream bwa-mem2 v2.2.1 sources build under a modern clang. clang-16+ turned
# several long-standing C/C++ conformance warnings into hard errors by default:
#   C (clang wrapper): bwa-mem2's bundled ext/safestringlib calls abort() (and a
#     few libc functions) without a prototype -> -Werror=implicit-function-
#     declaration / int-conversion / incompatible-pointer-types. Downgrade to
#     warnings (the functions are real libc symbols; codegen-neutral).
#   C++ (clang++ wrapper): bwa-mem2's kseq.h/utils.cpp use the C++17-removed
#     `register` keyword -> -Werror=register. Downgrade (keyword is a no-op on
#     modern compilers; codegen-neutral).
# Baking the flags into the wrappers (rather than CXX="clang++ -Wno-register")
# makes them ride through `make multi`'s per-tier sub-make recursion, which
# passes CXX/CC *unquoted*. bwa-mem3 v0.6.0 and minibwa v0.4 build clean under
# clang-19 and are unaffected by the extra warning suppression.
RUN printf '#!/bin/sh\nexec clang-%s -Wno-error=implicit-function-declaration -Wno-error=int-conversion -Wno-error=incompatible-pointer-types "$@"\n' "${LLVM_VERSION}" > /usr/local/bin/clang && \
    printf '#!/bin/sh\nexec clang++-%s -Wno-register "$@"\n' "${LLVM_VERSION}" > /usr/local/bin/clang++ && \
    chmod +x /usr/local/bin/clang /usr/local/bin/clang++ && \
    clang --version | head -1
# autoconf-archive + libtool aren't strictly required by the current
# ext/htslib submodule, but they're on upstream's official prereqs list
# (fg-labs/bwa-mem3 PR #93 → docs/src/getting-started/installation.md) and an
# htslib bump could start depending on them. Defensive hygiene.
# NOTE: libdeflate-dev is intentionally NOT installed here, before the upstream
# bwa-mem2 v2.2.1 build below. Upstream's bundled htslib auto-detects libdeflate
# and uses it, but the bwa-mem2 link step does not add -ldeflate, producing
# undefined-reference errors at link time. fg-labs/bwa-mem3 main, by contrast,
# requires libdeflate directly (src/fast_reader.c #include <libdeflate.h>,
# links -ldeflate). So libdeflate-dev is installed in a dedicated RUN *between*
# the upstream and fg-labs builds (see below) — present for fg-labs, absent for
# upstream. The runtime stage ships libdeflate0 for the resulting dynamic link.

# Install Rust toolchain (pinned to match rust-toolchain.toml).
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
ENV PATH=/opt/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain 1.85.0 --profile minimal

WORKDIR /build

# Build upstream bwa-mem2. Upstream v2.2.1 only has x86 SIMD (SSE4.1, AVX,
# AVX2, AVX-512BW); skip the build on arm64 so no `bwa-mem2.upstream` exists
# there. The workflow won't ask for it on arm archs (see BASELINE_ARCHS in
# workflow/Snakefile). Showing upstream has no ARM story is one of the points
# of this benchmark.
RUN if [ "$TARGETARCH" != "arm64" ]; then \
        git clone --depth 1 --branch ${UPSTREAM_TAG} --recursive ${UPSTREAM_REPO} upstream && \
        # Upstream v2.2.1 re-declares __rdtsc in src/utils.h which also exists in
        # GCC's <x86intrin.h>. GCC 12+ rejects the redefinition. Strip upstream's
        # copy so GCC's stdlib version is used (semantically identical — both
        # wrap the x86 RDTSC instruction).
        sed -i '/^static inline unsigned long long __rdtsc/,/^}$/d' upstream/src/utils.h && \
        cd upstream && \
        make multi -j"$(nproc)" CXX=clang++ CC=clang && \
        install -Dm755 bwa-mem2 /out/bin/bwa-mem2.upstream && \
        for v in sse41 avx avx2 avx512bw; do \
            [ -f bwa-mem2.$v ] && install -Dm755 bwa-mem2.$v /out/bin/bwa-mem2.upstream.$v || true; \
        done; \
    fi

# libdeflate-dev for the fg-labs/bwa-mem3 build only. Installed AFTER the
# upstream bwa-mem2 build above (which breaks if htslib links libdeflate) and
# BEFORE the fg-labs build below (which requires <libdeflate.h> + -ldeflate via
# src/fast_reader.c, added on main by the content-detecting FASTQ reader fast
# path). The runtime stage already ships libdeflate0 for the dynamic link.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libdeflate-dev \
    && rm -rf /var/lib/apt/lists/*

# NOTE: the fg-labs/bwa-mem3 clone + build is deliberately the LAST step in this
# stage (moved below, after compare-bams / minibwa / tricord / holodeck).
# FG_LABS_SHA is the one build input that changes on every release iteration, and
# Docker's per-stage layer cache is a linear chain — any layer *after* the
# invalidated one rebuilds regardless of its own inputs. Keeping the SHA-keyed
# fg-labs build last means a bwa-mem3-only SHA bump rebuilds just that block; the
# SHA-independent tool builds above (incl. the slow tricord/holodeck cargo
# installs) stay cached. libdeflate-dev (installed above, after the upstream
# bwa-mem2 build) is already present for the fg-labs build.

# Build compare-bams.
COPY Cargo.toml Cargo.lock rust-toolchain.toml /src/
COPY tools/compare-bams /src/tools/compare-bams
RUN cd /src && cargo build --release --bin compare-bams && \
    install -Dm755 /src/target/release/compare-bams /out/bin/compare-bams

# Build lh3/minibwa from the vendored submodule (vendor/minibwa, populated on
# the host via `git submodule update --init`). Single binary; ksw2 has a
# portable SIMD layer (NEON on arm64 via s2n-lite.h, SSE4.2 on x86 — minibwa
# has no AVX path yet). The submodule pin captures the source SHA; MINIBWA_SHA
# is set only as a build-arg label for the resulting image.
#
# The aarch64 GCC-12 s2n-lite.h type-mismatch on _mm_add_epi32 / _mm_sub_epi32
# (unsigned vaddq_u32 reinterpreted via signed vreinterpretq_u8_s32) that
# earlier pins needed a sed patch for was fixed upstream as of the pinned SHA
# (s2n-lite.h now uses vaddq_s32/vsubq_s32 consistently), so no patch is
# applied — the source builds cleanly on GCC 12+ for both arches.
COPY vendor/minibwa /build/minibwa
RUN cd /build/minibwa && \
    make clean && \
    make -j"$(nproc)" CC=clang && \
    install -Dm755 minibwa /out/bin/minibwa

# Install tricorder (fg-labs/tricord) for benchmarking the alignment subprocess
# without snakemake's `benchmark:` directive. Tricord requires Rust 1.94+, so
# install a separate stable toolchain rather than retraining 1.85 above. We
# pin to a published crates.io version for reproducibility; bump as needed.
ARG TRICORD_VERSION=0.1.2
RUN rustup toolchain install stable --profile minimal --no-self-update && \
    cargo +stable install --locked --root /out --version "${TRICORD_VERSION}" tricord

# Install holodeck (fg-labs/holodeck) for the truth-based accuracy benchmark:
# it generates the simulated truth datasets and provides `holodeck eval`. The
# repo is public, so cargo-install it from a pinned git ref (no submodule).
# Build with +stable (already installed for tricord, >=1.94): the +toolchain
# override takes precedence over holodeck's rust-toolchain.toml pin, avoiding a
# hard toolchain coupling. holodeck's `built` build-dependency links libgit2,
# hence libgit2-dev in the builder apt (build-only; the binary does not link it).
ARG HOLODECK_REPO
ARG HOLODECK_REF
RUN cargo +stable install --locked --git "${HOLODECK_REPO}" --rev "${HOLODECK_REF}" \
    --root /out holodeck

# Build fg-labs bwa-mem3 — intentionally the LAST builder step (see the NOTE
# above the compare-bams build for the layer-cache rationale). Fetch all branches
# so ${FG_LABS_SHA} can point to any branch (e.g. fg-main) or a release-please
# branch, not just the repo's default.
RUN git clone --recursive ${FG_LABS_REPO} fg-labs && \
    cd fg-labs && \
    git fetch --all --quiet && \
    git checkout ${FG_LABS_SHA} && \
    git submodule update --init --recursive && \
    # Same GCC-12 __rdtsc redefinition fix as upstream (no-op if fg-labs already
    # patched). The `|| true` is scoped to JUST the sed via the brace group — a
    # failing `git checkout` (e.g. a stale/orphaned FG_LABS_SHA after a PR
    # force-push) MUST fail the build, not get swallowed and silently fall back
    # to the default-branch HEAD, shipping a binary that isn't the requested SHA.
    { sed -i '/^static inline unsigned long long __rdtsc/,/^}$/d' src/utils.h || true; }
# Build fg-labs bwa-mem3. Since fg-labs PR #83, x86 ships a single binary
# with all five SIMD tiers (sse41/sse42/avx/avx2/avx512bw) compiled in and
# selected at runtime via in-process dispatch — no more `make multi` launcher
# + per-tier binaries. ARM is unchanged: still one binary, runtime-detected.
# The output filename was also renamed `bwa-mem2` → `bwa-mem3` (PR-rename).
# We install it under the legacy `bwa-mem2.fg-labs` path so the workflow
# rules that invoke `bwa-mem2.fg-labs mem` keep working untouched.
RUN cd fg-labs && \
    BUILT_BIN=bwa-mem3 && \
    case "${FG_LABS_MAKE_TARGET:-}" in \
      "" ) \
        if [ "$TARGETARCH" = "arm64" ]; then \
            make arch=arm64 -j"$(nproc)" CXX=clang++ CC=clang; \
        else \
            make BASELINE_ARCH="${BASELINE_ARCH:-avx2}" -j"$(nproc)" CXX=clang++ CC=clang; \
        fi ;; \
      lto-build ) \
        # `make lto-build` produces `bwa-mem3.lto`. LTO_ARCH selects the arch
        # flag passed to the inner make (Makefile defaults to `native`, which
        # would resolve to the build host's microarch rather than the bench
        # target's — set it explicitly so the LTO binary matches the
        # corresponding non-LTO build on the same arch.
        if [ "$TARGETARCH" = "arm64" ]; then \
            make lto-build LTO_ARCH=arm64 -j"$(nproc)" CXX=clang++ CC=clang; \
        else \
            make lto-build LTO_ARCH="${BASELINE_ARCH:-avx2}" -j"$(nproc)" CXX=clang++ CC=clang; \
        fi && \
        BUILT_BIN=bwa-mem3.lto ;; \
      * ) \
        echo "ERROR: unsupported FG_LABS_MAKE_TARGET=${FG_LABS_MAKE_TARGET} (supported: '' / lto-build)" >&2 ; \
        exit 1 ;; \
    esac && \
    install -Dm755 "$BUILT_BIN" /out/bin/bwa-mem2.fg-labs

############ Stage 2: runtime ############
FROM --platform=$TARGETPLATFORM debian:bookworm-slim AS runtime

ARG SAMTOOLS_VERSION
ARG BWA_VERSION
ARG BWAMETH_VERSION

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git unzip libz1 libbz2-1.0 liblzma5 libdeflate0 libncurses6 libgomp1 \
        python3 python3-pip python3-setuptools awscli time procps \
        build-essential libz-dev libbz2-dev liblzma-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python
# `python` symlink: snakemake worker invocations use `python -m snakemake`, which
# is not installed by Debian's `python3` package (they only provide `python3`).

# LLVM OpenMP runtime for the clang-built aligners. bwa-mem2/bwa-mem3/minibwa
# are compiled with clang -fopenmp, so each binary has a DT_NEEDED on
# libomp.so.5 and won't load without it. bookworm ships only libomp5-14; pull
# the exact-matching libomp5-19 from apt.llvm.org (same repo the builder uses)
# to avoid any OpenMP-runtime symbol mismatch. libgomp1 (above) stays for the
# gcc-built runtime `bwa` (lh3/bwa).
ARG LLVM_VERSION=19
RUN wget -qO /etc/apt/trusted.gpg.d/apt.llvm.org.asc https://apt.llvm.org/llvm-snapshot.gpg.key && \
    echo "deb http://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-${LLVM_VERSION} main" \
        > /etc/apt/sources.list.d/llvm.list && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libomp5-${LLVM_VERSION} && \
    rm -rf /var/lib/apt/lists/*

# samtools: build from source
RUN curl -L -o /tmp/samtools.tar.bz2 https://github.com/samtools/samtools/releases/download/${SAMTOOLS_VERSION}/samtools-${SAMTOOLS_VERSION}.tar.bz2 \
 && cd /tmp && tar xjf samtools.tar.bz2 && cd samtools-${SAMTOOLS_VERSION} \
 && ./configure --prefix=/usr/local --without-curses && make -j"$(nproc)" && make install \
 && rm -rf /tmp/samtools*

# bwa: clone + build
RUN git clone --depth 1 --branch v${BWA_VERSION} https://github.com/lh3/bwa /tmp/bwa \
 && cd /tmp/bwa && make -j"$(nproc)" && install -m755 bwa /usr/local/bin/ \
 && rm -rf /tmp/bwa*

# Free build-only packages (build-essential etc.) to reduce final image size
RUN apt-get update && apt-get purge -y --auto-remove \
        build-essential libz-dev libbz2-dev liblzma-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/*

# bwameth.py (pure Python wrapper) — not on PyPI; install from GitHub source.
RUN git clone --depth 1 --branch v${BWAMETH_VERSION} https://github.com/brentp/bwa-meth /tmp/bwameth \
 && pip3 install --no-cache-dir --break-system-packages /tmp/bwameth \
 && rm -rf /tmp/bwameth

# Binaries from builder stage
COPY --from=builder /out/bin/ /usr/local/bin/

# bwameth.py internally invokes a bare `bwa-mem2` command (see bwa-meth source).
# We install the suffixed variants (bwa-mem2.upstream, bwa-mem2.fg-labs) and have
# no plain `bwa-mem2` — bwameth baseline runs fail with "bwa-mem2: not found".
# Symlink bwa-mem2 -> bwa-mem2.upstream on amd64 (the only arch with upstream);
# arm64 doesn't have upstream and doesn't need the symlink because fg-labs's
# native --meth path doesn't shell out to bwa-mem2.
RUN if [ -f /usr/local/bin/bwa-mem2.upstream ]; then \
        ln -sf /usr/local/bin/bwa-mem2.upstream /usr/local/bin/bwa-mem2; \
        for v in sse41 avx avx2 avx512bw; do \
            [ -f /usr/local/bin/bwa-mem2.upstream.$v ] \
                && ln -sf /usr/local/bin/bwa-mem2.upstream.$v /usr/local/bin/bwa-mem2.$v \
                || true; \
        done; \
    fi

# Python CLI
COPY bwa_mem3_bench /opt/bwa_mem3_bench
COPY pyproject.toml /opt/pyproject.toml
RUN pip3 install --no-cache-dir --break-system-packages /opt

# Snakemake + AWS Batch executor for running inside the coordinator job.
# snakemake-storage-plugin-s3 is pinned to 0.3.1: from 0.3.3 onward the PyPI
# metadata requires snakemake-interface-storage-plugins >= 4.2.3, which in turn
# needs snakemake >= 9. We're on 8.x. 0.3.1 still declares the 3.x interface
# and works with snakemake 8.
# auto_deploy_default_storage_provider=False in our executor fork means workers
# rely on this system install rather than a per-worker `pip install`.
RUN pip3 install --no-cache-dir --break-system-packages \
    'snakemake>=8.27,<9' \
    'snakemake-interface-storage-plugins>=3.2.4,<4' \
    'snakemake-storage-plugin-s3==0.3.1' \
    'git+https://github.com/nh13/snakemake-executor-plugin-aws-batch.git@727782c961e96c2a50f934c526072357a22cdf07'

# Workflow + config so snakemake can find them without a checkout.
COPY workflow /opt/workflow
COPY config /opt/config
# build-arg-defaults.env carries the canonical MINIBWA_SHA, which the Snakefile
# reads at parse time via bwa_mem3_bench.minibwa_sha() (REPO_ROOT is /opt in the
# image). Without it every coordinator run — not just minibwa targets — would
# fail to parse the Snakefile.
COPY docker/build-arg-defaults.env /opt/docker/build-arg-defaults.env

# Coordinator entrypoint: reads FG_LABS_SHA + TARGET env vars and invokes snakemake.
# NOTE: no ENTRYPOINT — snakemake-registered worker jobs run `/bin/bash -c "..."`.
# With an ENTRYPOINT of /bin/bash those would become `/bin/bash /bin/bash -c ...`,
# which fails because bash tries to exec itself as a script. The coordinator job
# definition passes the entrypoint script directly (its shebang makes it runnable).
COPY docker/coordinator-entrypoint.sh /usr/local/bin/coordinator-entrypoint.sh
RUN chmod +x /usr/local/bin/coordinator-entrypoint.sh

# Host-identity capture, invoked from inside the align rules (NOT as a separate
# snakemake rule — see the script header for why that records the wrong machine).
COPY docker/emit-host-meta.sh /usr/local/bin/emit-host-meta
RUN chmod +x /usr/local/bin/emit-host-meta
