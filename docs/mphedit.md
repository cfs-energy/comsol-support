# edit-mph — load an existing .mph, optionally mutate, optionally solve, save

Sister to [`mphgen`](mphgen.md). Where `mphgen` is **builder → .mph**,
`edit-mph` is **existing.mph → mutate → .mph**. Both share one Java
entry point (`ModelExporter`) so solve, telemetry, partial-save, and
post-build linting plumbing is identical across modes.

Use this when a campaign starts from an inherited model — a prior run's
output, a lab partner's `.mph`, a reference repo, or a commercial
library — and iterates with single-change edits across cycles. The
"cycle 1 cost" of authoring a one-off `ModelUtil.load`+`save` harness
goes away.

**Meshing campaigns:** mutators that build meshes have their own
execution-semantics traps (`ms.run()` commit behavior, sequence
ordering, swept sourcing, quality measures). Read
[meshing.md](meshing.md) before authoring a mesh mutator. For
stage-by-stage modeling guidance (invariants, pitfalls, API quick-refs)
see [modeling-practice.md](modeling-practice.md); for the TagRegistry /
SelectionAlgebra naming conventions see
[facade-conventions.md](facade-conventions.md).

## The mutator contract

A mutator is optional — `edit-mph` is happy to load and re-save a model
unchanged (the canonical "solve an existing pre-solve .mph" workflow,
see below). When a mutator is supplied, it must expose:

```java
public static com.comsol.model.Model mutate(
    com.comsol.model.Model m,
    java.util.Map<String,String> args)
```

Requirements:
- **Returns** a `Model` — usually the same instance you received,
  possibly with mutations applied. Returning a fresh `Model` from
  `ModelUtil.create(...)` is allowed but unusual.
- **Does not** call `model.save(...)` or `ModelUtil.disconnect()`. The
  harness handles both, plus the save-on-exit safety net.
- **Does not** solve. Add `--solve <study_tag>` if you want a solved
  output; the same flag works in `mphgen`.
- **Reads arguments** from the `args` map. Missing keys should fall
  back to sensible defaults; invalid values should either coerce or
  be logged and defaulted — do not raise unless necessary.

See `tests/fixtures/mphgen/IdentityMutator.java` for a 5-line example.

## CLI

```bash
comsol-support edit-mph \
    --input path/to/existing.mph \
    --output path/to/edited.mph \
    [--mutator path/to/Mutator.java] \
    [--arg key=value ...] \
    [--solve STUDY_TAG] \
    [--no-save] \
    [--license-timeout SECONDS] \
    [--force] \
    [--timeout 600] \
    [--no-sidecar] \
    [--no-lint | --skip-layer-{a,b,c}]
```

Defaults:
- `--output`: required (no default). May equal `--input` for in-place edit.
  Under `--no-save` it only names the sidecars (no `.mph` is written).
- **In-place edits are atomic.** When
  `output == input`, the JVM writes to a temp sibling
  (`<name>.mph.inplace-tmp.mph`); only after the new file passes zip
  validation is it `os.replace`d over the input. A JVM death mid-save
  (timeout kill, OOM, full disk) can therefore never corrupt the only
  copy of the model — the input survives byte-identical, and the temp
  is cleaned up on every failure path.
- `--workspace`: mutator's directory if given, else output's directory.
- `--timeout`: 600 s on the JVM subprocess (bump higher for `--solve`).
- `--no-save`: load (+mutate/+solve) and report via telemetry, but **do
  not write the output `.mph`** — for fast read-only diagnostics on large
  models without paying the multi-GB write. Skips the runtime lint passes
  (nothing on disk to open) and produces no partial-save on failure. For
  a structured read-only return value, see
  [query-mph.md](query-mph.md), which builds on this.
- `--license-timeout`: bound the COMSOL license checkout (initStandalone
  + load) in seconds so a run with no free seat fails fast as
  `halt_reason=license_timeout` instead of blocking; falls back to
  `$COMSOL_LICENSE_TIMEOUT`, omitted/0 = wait indefinitely. See
  [run-harness.md](run-harness.md).
- **Mutator is optional.** Omit it to load → save unchanged. Combine with
  `--solve <tag>` to solve an existing pre-solve model.
- `--mesh MESH_TAG`: run the named mesh sequence
  first-class — whole-sequence `ms.run()` (the only reliable commit
  point), after any mutator and before any solve/query, with
  `mesh_heartbeat` liveness (incl. `rss_bytes`), a swallowed-but-
  reported failure path (a partial mesh still saves; the failure
  arrives as a `mesh_error` event + a result/`mesh_success=false`), an
  automatic post-run element census (`mesh_census` — score the
  artifact, not the exception) and per-feature build records
  (`mesh_feature_build`). See [meshing.md](meshing.md).
- `--jvm-arg FLAG` (repeatable): extra JVM flags for the
  ModelExporter JVM (e.g. `-Xmx96g` for large meshes). Previously only
  `run-harness` accepted this.
- A `<output>.mph.jvm.log` sidecar tees the FULL raw JVM
  stream — COMSOL solver/mesh log, SIGQUIT thread dumps, the final
  envelope — to disk line-by-line as it arrives, so a killed or hung
  run still leaves evidence. An empty file means the process emitted
  nothing before it ended (never cite absence of output as a
  measurement); failure messages name the log and flag emptiness.
- A `<output>.mphedit.json` sidecar is written with provenance (input
  sha256, output sha256, args, mutator, solve flag, telemetry digest);
  suppress with `--no-sidecar`.
- Post-edit linting runs Layer A on the mutator source (if any), Layer
  B on the output, Layer C on Layer B's symbols. Layer A is auto-skipped
  when no mutator is supplied (no .java source to scan).

## Python API

```python
from comsol_support.edit_mph import edit_mph

result = edit_mph(
    input_mph="path/to/existing.mph",
    output_mph="path/to/edited.mph",
    mutator_java="path/to/Mutator.java",   # optional
    mutator_args={"slot_pitch": "0.05"},
    solve_study=None,                      # or "std1" to solve before save
)
print(result.output_sha256, result.elapsed_ms, result.solved)
```

Raises (shares the `mphgen` exception hierarchy so callers handle one set):
- `ContractError` — mutator does not expose `mutate(Model, Map)`.
- `BuildFailure` — ModelExporter returned a failure envelope.
- `MphValidationError` — input or output is not a valid COMSOL .mph.
- `MphgenError` — other local errors (missing file, etc.).

## Three canonical patterns

### Pattern A — solve an existing pre-solve `.mph`

Pure load → solve → save. No mutator. Good for taking a `mphgen`-built
unsolved model and producing a solved counterpart in a controlled
environment (with telemetry, partial-save-on-failure, post-build lint).

```bash
comsol-support edit-mph \
    --input model_unsolved.mph \
    --output model_solved.mph \
    --solve std1
```

### Pattern B — apply a parameterized edit across cycles

Single-change mutator, varied `--arg` values per cycle. The mutator
itself stays version-controlled; campaign cycles set parameters via
`--arg`. This is the common case for parameter sweeps that start from
a fixed reference model.

```bash
for pitch in 0.030 0.040 0.050; do
  comsol-support edit-mph \
      --input reference.mph \
      --output sweep_pitch_${pitch}.mph \
      --mutator SetSlotPitch.java \
      --arg pitch=${pitch} \
      --solve std1
done
```

### Pattern C — chained mutations

Mutators compose by running `edit-mph` repeatedly with output of one
run feeding the input of the next. Use `--force` (or distinct output
paths) to handle re-runs.

```bash
comsol-support edit-mph --input ref.mph --output step1.mph \
    --mutator AddBoundaryCondition.java
comsol-support edit-mph --input step1.mph --output step2.mph \
    --mutator RefineMesh.java --arg level=2
comsol-support edit-mph --input step2.mph --output final.mph \
    --solve std1
```

## Telemetry envelope

Identical to `mphgen`. Sidecar JSONL at `<output>.telemetry.jsonl`,
events in order:

| Event | Edit-mode meaning |
|---|---|
| `run_start` | Includes `mode=edit`, input, mutator, output, solve_study |
| `load_done` | `ModelUtil.load` succeeded |
| `mutate_done` | Mutator returned a `Model` (omitted if no mutator) |
| `solve_start`, `solve_done`, `solver_heartbeat`, `solver_info` | Same as mphgen |
| `mesh_start`, `mesh_heartbeat`, `mesh_done`, `mesh_census`, `mesh_feature_build` | **`--mesh` verb.** Heartbeats carry `rss_bytes` + heap; `mesh_census` (always emitted, throw or not) carries per-type element counts + meshed/unmeshed domain counts and lands on the digest (`digest.mesh_census`); `mesh_feature_build` harvests each feature's `buildinfo`/`buildoutput`/`builddetails` (they survive the aggregate throw). A build exception becomes a `mesh_error` event — the partial mesh still saves. |
| `result_probe`, `result_global` | Same as mphgen |
| `save_done` | `model.save()` succeeded |
| `save_skipped` | Emitted instead of `save_done` under `--no-save`/query mode |
| `query_result` | The `query(...)` payload (query mode only) — see [query-mph.md](query-mph.md) |
| `partial_save` | Emitted on any failure path before `save_done` (suppressed under `--no-save`) |
| `<anything>_error` (e.g. `mesh_error`) | **Mutator-emitted.** A mutator that swallows an exception (mesh builds that must preserve a partial mesh — [meshing.md](meshing.md) §7) reports it via `SolverTelemetry.emitError("mesh_error", e)`. The digest counts these (`error_events`) and accumulates their detail (`error_event_detail`); edit-mph logs a one-line warning when a "successful" run carried them. `SolverTelemetry` is always on the mutator's compile/run classpath — no inlining needed. |
| `jvm_shutdown` | **Last-gasp.** Emitted by a JVM shutdown hook when the process is torn down *before* the normal `halt` (e.g. SIGTERM from a timeout kill) — payload carries `rss_bytes`. SIGKILL/SIGSEGV/SIGABRT cannot run hooks; for those the Python failure message decodes the signal and looks for `hs_err` dumps, and the digest's `last_event_type` (e.g. a heartbeat) shows the stream died mid-run. `solver_heartbeat` events also carry `rss_bytes` + JVM heap fields now. |
| `halt` | Final event; `halt_reason ∈ {success, load_error, contract_error, mutate_error, solver_error, query_error, save_error, license_timeout, exception}` |

### Detailed error capture (`error_detail`)

On any failure, the `solve_done` (status=error), `halt`, and final JSON
envelope carry an `error_detail` array — COMSOL's *real* diagnostic
lines (the failing feature, undefined variable, empty selection, NaN
DOF), harvested reflectively from the exception chain via
`getTranslatableMessageArray()`/`getMessages()`. `FlException.getMessage()`
alone is generic ("The following feature has encountered a problem"); the
detail array is where the root cause actually lives. It surfaces in the
`BuildFailure` message, the telemetry digest (`error_detail`), and
`EditMphResult.telemetry_error_detail`. The lines are kept verbatim
(COMSOL's raw layout tokens included) to avoid dropping signal.

### Known-gotcha hints on failure

On a non-zero exit, the CLI additionally runs the failure text through
the known-gotchas catalog (`suggest_for_error`) and prints up to three
matching `G-…` entry ids + symptom lines to stderr:

```
edit-mph error: ModelExporter failed: …
possibly related known gotchas:
  G-DISCONNECT-HANGS — ModelUtil.disconnect() hangs indefinitely …
  (details: comsol-support gotcha-search '<id>')
```

Matching is token-based against entry ids and Symptom lines only
(body-only matches are too noisy); the hints are advisory, never
change the exit code, and are skipped entirely if the catalog is
missing. The same lookup is available to MCP consumers as the
`search_gotchas` tool.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | edit-mph-local error (file not found, JVM error, etc.) |
| 2 | Bad CLI arguments |
| 3 | Contract error (mutator missing `mutate`, input/output mutual exclusion) |
| 4 | .mph validation error (zip structure, size — either input or output) |

## Difference vs. `mphgen`

| | `mphgen` | `edit-mph` |
|---|---|---|
| Source | `.java` builder | existing `.mph` |
| Contract method | `buildModel(Map)` (required) | `mutate(Model, Map)` (optional) |
| Sidecar suffix | `.mphgen.json` | `.mphedit.json` |
| Layer A target | Builder `.java` (always) | Mutator `.java` (when supplied) |
| In-place ok? | No (output is new) | Yes (`output == input`) |
| Telemetry, solve, partial-save | Same plumbing — both call `ModelExporter` |

## Testing

```bash
# Unit tests (no COMSOL):
uv run pytest tests/test_edit_mph.py

# E2E against a live COMSOL install:
COMSOL_E2E=1 uv run pytest tests/test_real_comsol_integration.py
```
