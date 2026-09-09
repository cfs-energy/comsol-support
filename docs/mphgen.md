# mphgen — GUI-openable .mph files from COMSOL Java builders

Generate a GUI-openable `.mph` file from any exploration-produced COMSOL
Java builder, deterministically and without any agentic reasoning.

See also: [modeling-practice.md](modeling-practice.md) (stage-by-stage invariants, pitfalls, API quick-refs) and [facade-conventions.md](facade-conventions.md) (TagRegistry / SelectionAlgebra conventions for builders).

Tier 1: the deterministic floor. Legacy builders (without the contract)
are rejected with a clear error; retrofit is a one-method addition.

> **Starting from an existing `.mph` instead of a `.java` builder?**
> `mphgen` does **not** accept an existing `.mph`. Use
> [`edit-mph`](mphedit.md) for the load → mutate → save lifecycle. It
> shares mphgen's `ModelExporter` Java entry point so solve, telemetry,
> partial-save, and post-build linting plumbing are identical across
> the two modes. Pattern A in `docs/mphedit.md` covers the common case
> of solving an existing pre-solve model with no mutator at all.

## The builder contract

Any builder that `mphgen` can consume must expose this exact signature:

```java
public static com.comsol.model.Model buildModel(
    java.util.Map<String,String> args)
```

Requirements:
- **Returns** an unsolved `Model` — geometry/mesh/physics built and ready.
- **Does not** call `model.save(...)` or `ModelUtil.disconnect()`. The
  harness handles both.
- **Does not** solve. `mphgen` writes the model pre-solve; users run
  studies in the COMSOL GUI (or with `--solve` in a future Tier).
- **Reads arguments** from the `args` map. Missing keys should fall
  back to sensible defaults; invalid values should either coerce or
  be logged and defaulted — do not raise unless absolutely necessary.

See `tests/fixtures/mphgen/SimpleBoxBuilder.java` for a minimal example.

## CLI

```bash
comsol-support mphgen \
    --builder path/to/Builder.java \
    --output path/to/output.mph \
    [--arg key=value ...] \
    [--solve STUDY_TAG] \
    [--force] \
    [--timeout 600] \
    [--no-sidecar]
```

Defaults:
- `--output`: `<ClassName>_unsolved.mph` next to the builder.
- `--workspace`: builder's directory (compiled `.class` files land there).
- `--timeout`: 600 s on the JVM subprocess (bump higher for `--solve`).
- **Default is unsolved.** Omit `--solve` to write a GUI-openable
  pre-solve model. The user clicks Compute in the GUI.
- **`--solve <study_tag>`** runs `model.study(<tag>).run()` before
  saving. Use only when a solved `.mph` is explicitly requested or
  a solved reference is load-bearing.
- A `<output>.mphgen.json` sidecar is written with provenance (builder
  sha256, output sha256, args, solve flag, elapsed_ms); suppress with
  `--no-sidecar`.

## Python API

```python
from comsol_support.mphgen import generate_mph

result = generate_mph(
    builder_java="path/to/Builder.java",
    output_mph="path/to/output.mph",
    builder_args={"size_m": "0.01"},
    solve_study=None,   # None (default) = unsolved; "std1" = solved
)
print(result.output_sha256, result.elapsed_ms, result.solved)
```

Raises:
- `ContractError` — builder does not expose `buildModel`.
- `BuildFailure` — ModelExporter returned a failure envelope.
- `MphValidationError` — output is not a valid COMSOL .mph.
- `MphgenError` — other mphgen-local errors (missing file, etc.).

## Retrofitting a legacy builder

Legacy exploration builders often have
a self-contained `main()` that builds, solves, exports, and removes the
model — they don't expose `buildModel` and they don't persist the model.
Two options to make them mphgen-compatible:

### Option A — add `buildModel` alongside `main` (15-line refactor)

Extract the pre-solve build steps from `runCase(ratio)` into a static
`buildModel` method. Keep `main` untouched — `buildModel` becomes an
additional, parameter-driven entry point.

```java
public static Model buildModel(Map<String,String> args) {
    double ratio = 0.50;
    if (args.containsKey("r")) ratio = Double.parseDouble(args.get("r"));
    Model model = ModelUtil.create("case_r" + ratio);
    // ... parameters, geometry, physics, mesh (no study.run() call)
    return model;
}
```

### Option B — write a sibling adapter (no edits to the legacy file)

Create `<BuilderName>Adapter.java` next to the legacy builder, reproduce
its pre-solve build steps, and expose `buildModel`. The original file
stays untouched — useful when the legacy builder is archival or shared.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | mphgen-local error (file not found, JVM error, etc.) |
| 2 | Bad CLI arguments |
| 3 | Contract error (builder missing `buildModel`) |
| 4 | .mph validation error (zip structure, size) |

On exit 1 the CLI also prints up to three known-gotcha hints matched
against the failure text (advisory, stderr-only — see the
"Known-gotcha hints" section in [mphedit.md](mphedit.md); both
commands share the same bridge).

## Testing

```bash
# Unit tests (mocked, no COMSOL):
uv run pytest tests/test_mphgen.py

# End-to-end against a live COMSOL install:
COMSOL_E2E=1 uv run pytest tests/test_mphgen.py -m real_comsol
```
