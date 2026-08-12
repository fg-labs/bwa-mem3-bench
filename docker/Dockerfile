# syntax=docker/dockerfile:1.7

############ Stage 1: builder ############
#
# Starts FROM the base image built by `docker/Dockerfile.base`, which carries the
# clang-19 toolchain, the Rust toolchains, the upstream bwa-mem2 v2.2.1 build and
# the four cargo-installed pinned tools (tricord, holodeck, fgumi, tachyon).
# Those inputs are independent of FG_LABS_SHA, so keeping them here meant every
# SHA bump either rebuilt them or depended on a local build cache surviving.
# As a base image they are an ordinary pull instead. See `Dockerfile.base` and
# `bwa_mem3_bench/base_image.py` for how the base tag is derived.
#
# BASE_IMAGE is deliberately defaultless: a wrong-but-plausible default would
# silently build against a stale toolchain, and there is no correct value that
# is not a function of the pins. `cli build` computes and passes it.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS builder

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

# Build fg-labs bwa-mem3 — intentionally the LAST builder step (see the NOTE
# above the compare-bams build for the layer-cache rationale).
#
# Fetch the SHA ITSELF, not `--all`. `git fetch --all` walks the configured
# refspecs, i.e. branches and tags, so it can only reach a commit that is
# currently the head of one. That silently excludes exactly the SHAs this bench
# blesses: a release-please branch head stops being reachable the moment the PR
# squash-merges and the branch is deleted, and every blessed golden is such a
# head (see docs/release-allowances.yaml — v0.5.0, v0.6.0, v0.7.0 and v0.8.0 all
# record the pattern). Rebuilding the v0.8.0 golden 4acb0956 failed with
# `fatal: reference is not a tree` for precisely this reason.
#
# GitHub serves any commit by SHA, so naming it directly works whether or not it
# is on a branch — and keeps working after the branch is gone.
# Quoted: these are build-args, so their values reach this shell verbatim and an
# unquoted expansion would let one carry shell metacharacters into the builder.
# `cli build` also refuses any FG_LABS_SHA that is not 40 hex chars, so this is
# the second of two layers rather than the only one.
RUN git clone --recursive "${FG_LABS_REPO}" fg-labs && \
    cd fg-labs && \
    git fetch --quiet origin "${FG_LABS_SHA}" && \
    git checkout "${FG_LABS_SHA}" && \
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
# Build-time provenance the runtime stage cannot regenerate — currently the
# tachyon probe's `rustc` (see the builder step that writes it). Read by
# `emit-host-probe`, which attaches it to every recorded score.
COPY --from=builder /out/share/ /usr/local/share/

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

# Host memory-contention probe, invoked from inside the thread-scaling ladder for
# the same reason emit-host-meta is invoked from inside the align rules: only the
# working shell is guaranteed to be on the working machine.
COPY docker/emit-host-probe.sh /usr/local/bin/emit-host-probe
RUN chmod +x /usr/local/bin/emit-host-probe
