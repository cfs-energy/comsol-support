# Finding-naming convention

Stable IDs for campaign findings. Used in `<CAMPAIGN_ID>_findings_register.md`
and cited verbatim across cycle logs, commit messages, and post-mortems.

## Format

```
F-<DOMAIN>-<PHENOMENON>-<SUFFIX>
```

Three required segments + an optional disambiguator:

| Segment | Constraint | Examples |
|---|---|---|
| `F-` | Literal prefix (never `f-`, never `FINDING-`) | `F-` |
| `DOMAIN` | UPPERCASE alpha; ≤8 chars; what part of the model/run it concerns | `SOLVER`, `MESH`, `PHYSICS`, `UNITS`, `IO`, `BUILD`, `PROBE`, `REFMATCH` |
| `PHENOMENON` | UPPERCASE alphanumeric; ≤16 chars; what's happening | `DIVERGENCE`, `MISMATCH-3X`, `OOM`, `HANG`, `STALE-INDEX` |
| `SUFFIX` (optional) | `-α/β/γ/…` for related variants, or `-N` for numeric disambiguation | `-α`, `-β`, `-2`, `-RESCUED` |

Suffix conventions:
- `α/β/γ` — Greek-letter siblings denote *related but distinct* findings.
  E.g. `F-SOLVER-DIVERGENCE-α` is the "Newton refusal at i=0.50" finding;
  `F-SOLVER-DIVERGENCE-β` is the *same kind* of divergence at i=0.95.
- Numeric suffixes — used only when α/β notation would be misleading
  (e.g. enumeration of independent occurrences, not variants).
- No suffix — the canonical case; the suffix appears later if a sibling
  is observed.

## Examples (illustrative)

| ID | Meaning |
|---|---|
| `F-SOLVER-DIVERGENCE-α` | Newton refuses to converge on a stationary step at one parameter value |
| `F-SOLVER-DIVERGENCE-β` | Same divergence pattern at a different parameter value (sibling of α) |
| `F-UNITS-MISMATCH-α` | Layer C flags `slot:<physics>/<feature>:<property>` as A/m^2 vs catalog 1/s |
| `F-PROBE-STALE-INDEX` | Boundary probe addresses entity 7 after a geometry edit promoted it to entity 12 |
| `F-REFMATCH-3X` | Outputs agree with a reference within 3× across the swept parameter range |
| `F-IO-DISCONNECT-HANG` | ModelUtil.disconnect() hangs ([G-DISCONNECT-HANGS](../docs/known-gotchas.md#g-disconnect-hangs) sibling — promote to known-gotcha if recurring) |

## When to add a SUFFIX

- **Promote to `-α`** when a second related finding emerges. Edit the
  original in place to add `-α`, then write the new sibling as `-β`.
- **Stay un-suffixed** if no sibling appears for ≥3 cycles. Suffixes
  are a tool for organizing variants, not a default.
- **Never use the same suffix twice** in the same campaign. Greek
  letters can repeat across campaigns (each campaign's namespace is
  fresh) but not within one.

## Anti-patterns

- ❌ `F-MISC-BUG-1` — `MISC` is not a domain, `BUG` is not a phenomenon.
- ❌ `F-solver-divergence-A` — case matters; lowercase + Latin suffix
  breaks grep patterns used across cycle logs.
- ❌ `F-CYCLE-12-FINDING-3` — IDs must describe the *content*, not the
  *location*; cycle logs already track location.
- ❌ Renaming an ID after it's been cited elsewhere — IDs are forever.
  If a finding turns out to be misnamed, mark it SUPERSEDED-BY and
  open a new ID with the better name.

## Quick template

When you observe something noteworthy mid-cycle:

```
F-<DOMAIN>-<PHENOMENON>  (no suffix yet — add α/β only if a sibling lands)
Status: OPEN
Opened: cycle N
Description: <one-line, ≤120 chars>
```

Drop it into `<CAMPAIGN_ID>_findings_register.md` and continue.
