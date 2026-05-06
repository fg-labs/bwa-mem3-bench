# syntax=docker/dockerfile:1.7

############ Stage 1: builder ############
FROM --platform=$TARGETPLATFORM debian:bookworm AS builder

ARG UPSTREAM_REPO
ARG UPSTREAM_TAG
ARG FG_LABS_REPO
ARG FG_LABS_SHA
ARG SAMTOOLS_VERSION
ARG BWA_VERSION
ARG BWAMETH_VERSION
ARG TARGETARCH

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential git cmake curl ca-certificates pkg-config \
        libz-dev libbz2-dev liblzma-dev libcurl4-openssl-dev libssl-dev \
        libncurses-dev \
        python3 python3-pip python3-venv \
        autoconf automake \
    && rm -rf /var/lib/apt/lists/*
# NOTE: libdeflate-dev intentionally omitted — bwa-mem2's bundled htslib detects it
# and uses it, but the bwa-mem2 link step does not add -ldeflate, producing
# undefined-reference errors at link time. Without libdeflate, htslib falls back
# to zlib only, which is sufficient for our use case.

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
        make multi -j"$(nproc)" && \
        install -Dm755 bwa-mem2 /out/bin/bwa-mem2.upstream && \
        for v in sse41 avx avx2 avx512bw; do \
            [ -f bwa-mem2.$v ] && install -Dm755 bwa-mem2.$v /out/bin/bwa-mem2.upstream.$v || true; \
        done; \
    fi

# Build fg-labs bwa-mem3. Fetch all branches so ${FG_LABS_SHA} can point to any
# branch (e.g. fg-main) not just the repo's default.
RUN git clone --recursive ${FG_LABS_REPO} fg-labs && \
    cd fg-labs && \
    git fetch --all --quiet && \
    git checkout ${FG_LABS_SHA} && \
    git submodule update --init --recursive && \
    # Same GCC-12 __rdtsc redefinition fix as upstream (no-op if fg-labs already patched).
    sed -i '/^static inline unsigned long long __rdtsc/,/^}$/d' src/utils.h || true
RUN cd fg-labs && \
    if [ "$TARGETARCH" = "arm64" ]; then \
        make arch=arm64 -j"$(nproc)" && \
        install -Dm755 bwa-mem2 /out/bin/bwa-mem2.fg-labs; \
    else \
        make multi -j"$(nproc)" && \
        install -Dm755 bwa-mem2 /out/bin/bwa-mem2.fg-labs && \
        for v in sse41 avx avx2 avx512bw; do \
            [ -f bwa-mem2.$v ] && install -Dm755 bwa-mem2.$v /out/bin/bwa-mem2.fg-labs.$v || true; \
        done; \
    fi

# Build compare-bams.
COPY Cargo.toml Cargo.lock rust-toolchain.toml /src/
COPY tools/compare-bams /src/tools/compare-bams
RUN cd /src && cargo build --release --bin compare-bams && \
    install -Dm755 /src/target/release/compare-bams /out/bin/compare-bams

# Install tricorder (fg-labs/tricord) for benchmarking the alignment subprocess
# without snakemake's `benchmark:` directive. Tricord requires Rust 1.94+, so
# install a separate stable toolchain rather than retraining 1.85 above. We
# pin to a published crates.io version for reproducibility; bump as needed.
ARG TRICORD_VERSION=0.1.0
RUN rustup toolchain install stable --profile minimal --no-self-update && \
    cargo +stable install --locked --root /out --version "${TRICORD_VERSION}" tricord

############ Stage 2: runtime ############
FROM --platform=$TARGETPLATFORM debian:bookworm-slim AS runtime

ARG SAMTOOLS_VERSION
ARG BWA_VERSION
ARG BWAMETH_VERSION

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip libz1 libbz2-1.0 liblzma5 libdeflate0 libncurses6 libgomp1 \
        python3 python3-pip python3-setuptools awscli time procps \
        build-essential libz-dev libbz2-dev liblzma-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python
# `python` symlink: snakemake worker invocations use `python -m snakemake`, which
# is not installed by Debian's `python3` package (they only provide `python3`).

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
    'git+https://github.com/nh13/snakemake-executor-plugin-aws-batch.git@c736516400538a45215a3527fcc20dc07251f245'

# Workflow + config so snakemake can find them without a checkout.
COPY workflow /opt/workflow
COPY config /opt/config

# Coordinator entrypoint: reads FG_LABS_SHA + TARGET env vars and invokes snakemake.
# NOTE: no ENTRYPOINT — snakemake-registered worker jobs run `/bin/bash -c "..."`.
# With an ENTRYPOINT of /bin/bash those would become `/bin/bash /bin/bash -c ...`,
# which fails because bash tries to exec itself as a script. The coordinator job
# definition passes the entrypoint script directly (its shebang makes it runnable).
COPY docker/coordinator-entrypoint.sh /usr/local/bin/coordinator-entrypoint.sh
RUN chmod +x /usr/local/bin/coordinator-entrypoint.sh
