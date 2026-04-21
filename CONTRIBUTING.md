# Contributing

Thanks for your interest in contributing to `bwa-mem3-bench`. This document
covers the local development workflow, the checks your changes need to pass,
and our conventions for branches, commits, and pull requests.

## Local development

[`pixi`][pixi] manages the development environment (Python + Rust toolchain
+ snakemake + conda packages).

[pixi]: https://pixi.sh/

Install dependencies and create the dev environment:

```bash
pixi install -e dev
```

Then run all checks (rust + python):

```bash
pixi run check
```

This is the same set CI runs. It expands to:

```bash
pixi run cargo-fmt-check    # rustfmt --check
pixi run cargo-clippy       # clippy --all-features --all-targets -D warnings
pixi run cargo-test         # cargo test --workspace

pixi run py-fmt-check       # ruff format --check
pixi run py-lint            # ruff check
pixi run py-type            # mypy --strict
pixi run py-test            # pytest
```

To auto-fix formatting:

```bash
pixi run cargo-fmt          # rustfmt --write
pixi run py-fmt             # ruff format
```

## Conventions

### Code style

- **Python** — [Fulcrum Python conventions][fg-py]: 100-char lines, ruff
  format + ruff lint + mypy strict, [defopt][defopt] for CLI tools, type
  annotations on every public function. The full ruleset is configured in
  `pyproject.toml`.
- **Rust** — pinned via `rust-toolchain.toml`; the workspace forbids
  `unsafe_code` and treats clippy warnings as errors. Format with `rustfmt`
  (max width 100). Prefer borrows and immutability; small modules over
  large ones. Annotate explicit generics on parameters
  (`fn f<P: AsRef<Path>>(p: P)`) over `impl Trait`.
- **Snakemake** — every rule has a name + docstring + `input` + `output` +
  `log`. Use keyword arguments only (no positional). Redirect stdout/stderr
  to `{log}` with `(...) &> {log}`.

[fg-py]:  https://github.com/fulcrumgenomics/python-template
[defopt]: https://github.com/anntzer/defopt

### Branches

Short, kebab-case, descriptive — e.g. `fix-coordinator-timeout`,
`add-c8g-arch`, `nh-meth-index-cache`.

Never commit directly to `main`. Use a branch and open a pull request.

### Commits

[Conventional Commits][conv-commits]:

```
feat: add support for c8g instance type
fix(workflow): handle empty FASTQ pairs without raising
docs: clarify hg38 reference setup in data-setup.md
chore: bump pixi.lock
```

Common types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`,
`perf`, `build`.

[conv-commits]: https://www.conventionalcommits.org/en/v1.0.0/

Do not include AI attribution in commit messages, PR titles, or PR
descriptions.

When staging files, prefer `git add <path>` over `git add -A` / `git add .`
to avoid accidentally committing local-only files (`.env`, `data-stage/`,
etc.).

### Pull requests

Open as a draft first; self-review your own diff before requesting reviews.
Address every review comment (inline, body, and any `CodeRabbitAI`
nitpicks) before merging.

The PR template covers the checklist; keep formatting and functional
changes in separate PRs when possible.

## Reporting issues

Use the bug-report or feature-request templates under
[Issues](../../issues/new/choose). Please confirm there isn't already an
open issue for the same problem before filing.

For benchmark-related discussion or questions, prefer
[Discussions](../../discussions) over Issues.
