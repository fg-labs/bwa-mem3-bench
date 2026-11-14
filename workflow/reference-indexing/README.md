# Reference indexing

Standalone Snakemake workflow for indexing existing, uncompressed FASTAs with the benchmark image's `bwa-mem2.fg-labs` executable. It does not run benchmarks or modify the benchmark DAG. Standard stride-8 indexes only: no methylation or dense-SA variants.

## Configuration and local runs

Copy `config.example.yaml` to a separate configuration file and replace the illustrative paths. `assemblies` maps unique names matching `[A-Za-z0-9][A-Za-z0-9._-]*` (names may not differ only in case) to absolute local paths or lowercase `s3://bucket/key` URIs of uncompressed `<name>.fa` or `<name>.fasta` files. Relative paths, empty/`.`/`..` components, control characters, any of `{}\?#%`, compressed inputs, unknown settings and invalid resource values are rejected when Snakemake parses the workflow, before any job starts; errors name the offending assembly and value. `config.schema.json` encodes the same rules for the biffix form. FASTA basenames are preserved exactly.

From the repository root, with the workflow environment on `PATH`:

```sh
snakemake --snakefile workflow/reference-indexing/Snakefile \
  --directory "$WORKDIR" --configfile "$CONFIG" --cores 16 --dry-run
snakemake --snakefile workflow/reference-indexing/Snakefile \
  --directory "$WORKDIR" --configfile "$CONFIG" --cores 16 --rerun-incomplete
```

Use absolute paths for `WORKDIR` and `CONFIG`. The default `binary`, `bwa-mem2.fg-labs`, is bwa-mem3 as installed in the benchmark image; this workflow does not build an image, and local runs do not need an image URI. For a local installation, set `binary` to a `bwa-mem3` executable (a name on `PATH` or an absolute path) that supports `version` and `index -t N --max-memory BYTES -p PREFIX`, plus `--emit-unpacked-ref` when `emit_unpacked_ref` is true. Python/Snakemake, Bash and `sha256sum` must be available. biffix runs this workflow on Snakemake 9.27.0 (`engine_version` in `biffix.yaml`); `pixi run -e sm9 py-test-reference-indexing` runs its tests on that engine.

Each assembly defaults to **16 threads and 98304 MiB (96 GiB)**; override `threads` and `mem_mb` in config. `mem_mb` is in MiB, as for every rule in this repo: the AWS Batch executor passes the rule's `mem_mb` resource unchanged as the container's memory, which Batch reads as MiB, so the default is a 96 GiB container limit. The indexer's `--max-memory` follows the job's resolved `mem_mb` (including `--set-resources` overrides) less the same reserve the indexer applies on its own, min(max(2 GiB, 5%), 50%): 91.2 GiB of the default 96 GiB. The build's preflight check therefore refuses a reference that will not fit, rather than being killed at the container limit. Reserve host memory above the requested 96 GiB for the OS and container overhead (a 128 GiB or larger host, not a 32 GiB `c8g.4xlarge`). These are standard human-reference starting budgets, not guarantees for larger assemblies. Ensure adequate scratch disk for staged inputs, indexes and indexer temporary files. Set Snakemake `--cores` at least as high as `threads`; for remote execution also keep the job/worker ceilings high enough to avoid CPU clamping.

## Artifacts and failures

All outputs are relative to the working directory locally, or to biffix's default per-run S3 output prefix remotely:

```text
indexes/<assembly>/<original-basename>.amb
indexes/<assembly>/<original-basename>.ann
indexes/<assembly>/<original-basename>.bwt.2bit.64
indexes/<assembly>/<original-basename>.pac
indexes/<assembly>/<original-basename>.index.log
indexes/<assembly>/<original-basename>.version.txt
indexes/<assembly>/<original-basename>.source.sha256
indexes/<assembly>/<original-basename>.checksums.sha256
```

`emit_unpacked_ref: true` additionally declares and requires `<original-basename>.0123` for consumers that require the unpacked reference (including bwa-mem2). It is absent from the DAG by default. Use a fresh working/output prefix when switching this option or image, so old optional artifacts cannot be mistaken for the current build.

The index command uses an explicit output prefix, never the source FASTA as its output prefix. Sources may be read-only. No FASTA copy, `.fai`, or dictionary is published. S3 inputs use explicit `storage.s3(...)`, while local inputs bypass default storage mapping. The outputs remain relative so the storage plugin maps every artifact into the run's output prefix. Keep input sources separate from that prefix.

All artifacts must be nonempty. The version command and indexer must exit successfully; missing or empty sidecars fail the job before checksums are written. Checksums cover the sidecars, version, source digest and build log with filenames relative to the assembly directory (`sha256sum -c <original-basename>.checksums.sha256` from that directory). `<original-basename>.source.sha256` records the sha256 of the FASTA that was indexed, in `sha256sum -c` format, so the source can be checked against the index wherever the FASTA sits next to it (for example after promotion). The build log is the rule's `log:`: Snakemake keeps it when the job fails, so the indexer's error survives, and default storage stages it with the outputs. Snakemake removes the outputs of a failed job; storage upload is not an atomic multi-object transaction, so use isolated run prefixes and treat only a successful workflow with all artifacts/checksums as complete. Never consume a partial remote prefix. Retry with `--rerun-incomplete`.

## biffix registration and execution

The manifest paths are relative to **this directory**, not the repository root. Register this subdirectory with S3 source mode; git source mode runs the repository root and is not suitable for this layout. All workflow code, schema and manifest files must first be tracked and committed. The source bundle stays inside this directory, with no `../` includes. Do not bundle input FASTAs.

Supply an **existing benchmark image** explicitly using `--image`. Use its immutable release/SHA tag or digest, never `latest`; the registration pins and imports that image and injects it as the default worker image. There is intentionally no fabricated registry URI or image-build recipe in the manifest and no rule-level image override that would bypass biffix's pinned image mapping.

Commands below are instructions only; registration/import and submission perform remote writes and need separate authorization:

```sh
biffix workflows form workflow/reference-indexing --output json
biffix workflows register workflow/reference-indexing \
  --name reference-indexing --project "$PROJECT" --source-mode s3 \
  --image "$IMAGE" --queue-class cpu-spot
biffix runs submit "$VERSION_ID" --params "$CONFIG" --name reference-indexing --follow
```

For on-demand execution, register a separate version using `--queue-class cpu-ondemand` instead. The first queue class registered selects the worker queue; there is no run submission queue switch. Do not use an architecture-specific benchmark queue. Both queue classes must offer instances with more than 96 GiB usable RAM, sufficient disk, and a CPU architecture compatible with the supplied image. Queue provisioning is external to this workflow; registration does not create or resize compute capacity. Set `VERSION_ID` to the ID returned by registration. Submit the assembly map using a YAML/JSON params file: the current biffix form displays this object as a text field, not a map editor. Validation of map entries runs when Snakemake parses the workflow.

Stage source FASTAs separately in an approved readable input location before launching. biffix automatically publishes declared job outputs into its per-run output prefix, but this workflow never writes to a shared reference catalog or final reference-data location. After successful validation, promotion of sidecars to their final location is a separate, explicitly approved operation. Preserve the source FASTA/FAI/dictionary there; verify checksums, the source digest against the destination FASTA, and consumer compatibility before promotion.
