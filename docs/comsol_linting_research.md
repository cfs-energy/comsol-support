# COMSOL Java API — linting research

**Date:** 2026-04-21
**Source:** `<COMSOL_PATH>/doc/help/wtpwebapps/ROOT/doc/com.comsol.help.comsol/api/` (local Javadoc, COMSOL 6.4)
**Method:** parsed `member-search-index.js` + class `.html` pages.

## Headline finding

**COMSOL's Java API does not expose the GUI yellow-highlight unit warnings.** There is no `feedbacks()`, `warnings()`, `messages()`, `ModelUtil.getFeedback()`, or any equivalent feedback-collector for expression unit analysis. The closest hits are all scoped to specific feature kinds (solver, batch, geom/mesh), not expressions.

Warnings are also not persisted in `.mph`, and this extends that: they are also not obtainable at runtime via the API. **The warnings are computed client-side by the GUI's own expression analyzer** (separate from the model server).

Both open questions therefore resolve to: **no accessible API.** The W1/W2/W3 regression cannot be satisfied by Layer B as originally conceived.

**Reinforced 2026-04-22:** three follow-up probes closed the remaining
deterministic-backend channels (`mphserver` RPC, `batch -methodcall`,
exhaustive flag/env sweep). None expose the warning channel. The Javadoc
scan below is therefore complete with strictly stronger confidence.

## Priority 1 — feedback/warning list API (BLOCKED)

| Searched | Found |
|---|---|
| `Model#feedbacks()` / `.warnings()` / `.messages()` | none |
| `ModelUtil.getFeedback()` / `listWarnings()` | none |
| `FeedbackList`, `MessageList`, `WarningList` classes | none |
| `*ProblemHandler*`, `*ErrorHandler*` at Model level | none |

Warning-related methods that DO exist:
- `GeomMeshFeature#hasWarning()` / `.warnings()` — geometry/mesh only
- `SolverFeature#hasWarning()` / `SolverSequence#getWarningMessage()` — solver only
- `BatchFeature#hasWarning()` / `.getWarningMessage()` — batch only
- `ResultBaseFeature#hasWarning()` — result features only

None of these surface expression unit-analysis warnings.

## Priority 2 — unit analysis trigger (PARTIAL)

**Found:** `com.comsol.model.ParamBase#evaluateUnit(String name)` — returns the deduced unit of a **global parameter**'s expression.

**Not found:** no `analyzeUnits`, `checkUnits`, `unitCheck`, `Model#check`, or `Model#validate`.

Behavior (to verify empirically during implementation): likely returns a unit string for consistent expressions; throws or returns empty for inconsistent ones.

**Critical limitation:** `evaluateUnit` is defined on `ParamBase` only. Variable collections (`Expr`, which holds variables inside a component) inherit from `ExpressionBase` + `ExpressionEntity` but NOT `ParamBase` — so there is **no deduced-unit API for variables**.

## Priority 3 — description API (CONFIRMED)

- `com.comsol.model.ExpressionBase#descr(String name)` — getter, returns `String`
- `com.comsol.model.ExpressionBase#descr(String name, String descr)` — setter (on ExpressionBase)
- `com.comsol.model.ExpressionEntity#descr(String name, String descr)` — setter (on ExpressionEntity; Expr extends both)
- `com.comsol.model.ExpressionEntity#set(String, String, String)` — 3-arg set (name, expr, descr)
- Unset description returns empty string `""` (not null) per COMSOL convention — verify empirically.

Works uniformly on:
- `model.param()` → ModelParam (implements ExpressionBase + ParamBase)
- `model.variable(tag)` → Expr (implements ExpressionBase + ExpressionEntity)

## Priority 4 — enumeration APIs (CONFIRMED)

- `ExpressionBase#varnames()` — returns `String[]` of all names defined in this container.
- `ComponentExprList.tags()` (inherited from `ModelEntityList`) — returns all variable-collection tags in a component.
- `Model#variable()` → `ComponentExprList` (collection of collections).
- `Model#variable(String tag)` → `Expr` (one collection).
- `Model#modelNode()` → ModelNodeList → `.tags()` → component tags.
- `Model#param()` — top-level global params, no tag needed.

Iteration pattern for all variables:
```java
for (String compTag : model.modelNode().tags()) {
  ModelNode comp = model.modelNode(compTag);
  for (String varTag : comp.variable().tags()) {
    Expr v = model.variable(varTag);
    for (String name : v.varnames()) {
      String expr = v.get(name);
      String descr = v.descr(name);
      // ...
    }
  }
}
```

## Minimum viable snippet (load a model and dump what we CAN get)

```java
import com.comsol.model.*;
import com.comsol.model.util.*;

public class DumpLint {
  public static void main(String[] args) throws Exception {
    ModelUtil.initStandalone(false);
    Model model = ModelUtil.load("m", args[0]);

    // Global parameters — full unit + descr
    String[] pnames = model.param().varnames();
    System.out.println("params: " + pnames.length);
    for (String n : pnames) {
      String expr = model.param().get(n);
      String descr = model.param().descr(n);
      String unit;
      try { unit = model.param().evaluateUnit(n); }
      catch (Exception e) { unit = "ERR:" + e.getMessage(); }
      System.out.printf("  %s = %s [%s] // %s%n", n, expr, unit, descr);
    }

    // Variables per component — expr + descr only (no unit API)
    for (String compTag : model.modelNode().tags()) {
      for (String vTag : model.modelNode(compTag).variable().tags()) {
        Expr v = model.variable(vTag);
        for (String n : v.varnames()) {
          System.out.printf("  %s/%s/%s = %s // %s%n",
            compTag, vTag, n, v.get(n), v.descr(n));
        }
      }
    }

    System.exit(0);
  }
}
```

## Revised Layer B scope (simplified)

Given Priority 1 is blocked, Layer B's realistic feature set is narrowed:

| Original Layer B target | Feasible? |
|---|---|
| W1 (power-of-unit warning on `rho_val`) | **NO** — variable, not param; no API |
| W2 (PDE slot mismatch `[Ohm]` vs `[1/m]`) | **NO** — requires physics-slot expected-unit walk + expression deduction for variables |
| W3 (PDE slot mismatch `[H/m]` vs `[s/m^2]`) | **NO** — same as W2 |
| Parameter unit errors (inconsistent param expressions) | **YES** — via `evaluateUnit` exception/empty-return |
| Missing descriptions on params | **YES** |
| Missing descriptions on variables | **YES** |
| Placeholder descriptions (TODO etc.) | **YES** |

**Concrete revised scope:**
- Enumerate all params (`model.param().varnames()`) and all variables (walk components/variable-collections).
- For each, record: name, expression (`.get()`), description (`.descr()`).
- For params only: call `evaluateUnit(name)`; if exception or empty result, record as unit error.
- Write `<mph>.units.json` with layer_b.errors (param-level only).
- Write `<mph>.descriptions.json` with layer_b.missing_runtime + placeholder lists.
- Document in `docs/linting.md` that PDE-slot GUI warnings (yellow) are **not detectable via Java API** — plan's Known Unknown Q1–Q5 confirmed as hard-blocked.

## Regression-test retarget

The original acceptance criterion (≥3 warnings matching W1/W2/W3) is **not achievable**. Replace with:
- Loads the reference `.mph` without crashing.
- Emits valid JSON sidecars matching the documented schemas.
- units.json enumerates every parameter and reports either a unit string or a captured error per param.
- descriptions.json enumerates every component variable — most will lack descriptions in an older builder and should appear in `missing_runtime`.

## Conclusion

Proceed with implementation but ship Layer B with narrowed scope. The core value of the feature remains:
- Layer A catches the frequent agentic-worker typos (`[m]/[s]`, `/[s]`, unicode in brackets) cheaply and offline.
- Layer B catches parameter unit errors and universally enforces descriptions at runtime.
- PDE slot semantic warnings remain a GUI-only signal; documented as a known limitation. Future work could scrape the GUI's expression analyzer, but that is explicitly deferred.
