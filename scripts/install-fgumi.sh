#!/usr/bin/env bash
# Install `fgumi` from the pinned git ref into .fgumi/ for the test suite.
#
# Why this is not a conda/pypi dependency: `fgumi compare bams` is feature-gated
# off in a default build (`--features compare`), so it has to come from source,
# and it must come from the SAME ref the runtime image uses or the dev binary
# and the image binary can disagree about what "identical" means. The pin is
# sourced from docker/build-arg-defaults.env rather than repeated here, so
# bumping FGUMI_REF is one edit.
#
# Run it through pixi (`pixi run install-fgumi`). fgumi requires rustc >= 1.93;
# the pixi environment's conda `rust` supplies 1.94, while a bare shell here
# gets rustup honouring rust-toolchain.toml's 1.85 pin and cannot build it. The
# Dockerfile solves the same problem with `cargo +stable`, which is a rustup
# directive and is NOT available against the conda cargo — hence a version
# check rather than a toolchain override.
set -euo pipefail

readonly MIN_MAJOR=1
readonly MIN_MINOR=93

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

readonly PIN_FILE="docker/build-arg-defaults.env"
if [[ ! -f "$PIN_FILE" ]]; then
    echo "ERROR: $PIN_FILE not found (looked in $repo_root)." >&2
    echo "       It carries FGUMI_REPO/FGUMI_REF, the pin this install must match." >&2
    exit 1
fi
# shellcheck source=/dev/null
. "$PIN_FILE"

if [[ -z "${FGUMI_REPO:-}" || -z "${FGUMI_REF:-}" ]]; then
    echo "ERROR: FGUMI_REPO/FGUMI_REF missing from $PIN_FILE" >&2
    exit 1
fi

# Both tools are checked, and separately. `rust-version` is a COMPILER
# requirement, so cargo's version cannot answer it: cargo resolves the compiler
# through `${RUSTC:-rustc}`, which a rust-toolchain.toml pin or an explicit
# RUSTC can point at an older rustc than `cargo --version` reports. Checking
# only cargo lets that combination past the preflight and fails later, mid-build,
# with an error naming a dependency instead of the toolchain.
require_tool() {
    # $1 = display name, $2 = binary to resolve.
    if ! command -v "$2" >/dev/null 2>&1; then
        echo "ERROR: $1 not found on PATH (looked for '$2')." >&2
        echo "       Run through pixi (\`pixi run install-fgumi\`), whose environment" >&2
        echo "       supplies the conda rust toolchain this script expects." >&2
        exit 1
    fi
}

# Publishes the version it read to REPORTED_VERSION rather than printing it.
# A `$(...)` capture would run the check in a subshell, where the `exit 1` below
# ends only that subshell -- the failure would then depend entirely on `set -e`
# noticing the assignment's status, which is far too subtle for a guard whose
# whole job is to stop the build.
REPORTED_VERSION=""
require_version() {
    # $1 = display name, $2 = binary. Both rustc and cargo print
    # `<name> <semver> (<hash> <date>)`, so the version is always field 2.
    local name="$1" bin="$2" major rest minor
    REPORTED_VERSION="$("$bin" --version | awk '{print $2}')"
    major="${REPORTED_VERSION%%.*}"
    rest="${REPORTED_VERSION#*.}"
    minor="${rest%%.*}"
    if ((major < MIN_MAJOR || (major == MIN_MAJOR && minor < MIN_MINOR))); then
        echo "ERROR: fgumi needs ${MIN_MAJOR}.${MIN_MINOR} or newer;" \
             "this ${name} is ${REPORTED_VERSION}." >&2
        echo "       Run through pixi (\`pixi run install-fgumi\`), whose conda rust is new enough." >&2
        echo "       A bare shell picks up rust-toolchain.toml's pin, which is older on purpose." >&2
        exit 1
    fi
}

require_tool cargo cargo
# Resolve the compiler exactly as cargo will, so the preflight validates the
# binary that actually builds rather than whatever `rustc` PATH happens to find.
#
# Cargo takes `RUSTC` ahead of `CARGO_BUILD_RUSTC` (the env spelling of the
# `build.rustc` config key), so the fallback chain has to be in that order --
# reversed, this would validate a compiler cargo is not going to run.
# `RUSTC_WRAPPER` is deliberately NOT consulted: it wraps the real compiler
# rather than replacing it, so it does not change the version in play.
#
# Not covered: `build.rustc` set in a `.cargo/config.toml`. Reading cargo's
# layered config here would mean reimplementing its resolution, and the env
# vars are what CI and pixi actually use.
readonly RUSTC_BIN="${RUSTC:-${CARGO_BUILD_RUSTC:-rustc}}"
require_tool rustc "$RUSTC_BIN"

require_version cargo cargo
cargo_version="$REPORTED_VERSION"
require_version rustc "$RUSTC_BIN"
rustc_version="$REPORTED_VERSION"

# Build zlib from libz-sys's vendored copy instead of linking a system one.
#
# fgumi pulls in git2 -> libgit2-sys -> libz-sys, whose default is a bare `-lz`.
# The pixi environment ships `libzlib` (runtime `libz.so.1`) but not `zlib`, so
# there is no `libz.so` to link, and the conda toolchain compiles against its own
# sysroot rather than the host's /usr/lib -- on Linux that is
# `rust-lld: unable to find library -lz` and the install dies in fgumi's build
# script. macOS gets libz from the SDK, which is why this built locally and
# failed only in CI.
#
# Adding `zlib` to pixi.toml would also fix it, but `pixi add` re-solves the
# whole lock (~9.6k lines here) for a dependency only this one script needs.
# LIBZ_SYS_STATIC is scoped to the crate that has the problem.
#
# This does NOT put the dev binary at odds with the image's, which links the
# system zlib: `compare bams` only ever decompresses, and inflate is defined by
# the format rather than by the implementation. A differing zlib could change
# what a writer PRODUCES; it cannot change what a reader SEES.
export LIBZ_SYS_STATIC=1

echo "Installing fgumi from ${FGUMI_REPO} @ ${FGUMI_REF}" \
     "(cargo ${cargo_version}, rustc ${rustc_version})" >&2
cargo install --locked \
    --git "$FGUMI_REPO" \
    --rev "$FGUMI_REF" \
    --features compare \
    --root .fgumi \
    fgumi

"$repo_root/.fgumi/bin/fgumi" --version >&2
