# Linter Deferred Backlog

Low-priority linter enhancements surfaced during validation cycles.
Batched here to avoid per-cycle polish noise. Address in a dedicated
linter-polish cycle.

## Entry 1 — `_ARG_SPLIT` does not recognize Java expressions as the 3rd argument — **RESOLVED**

**Resolution.** `linting.py` now splits `.set(...)` arguments on
top-level commas (string-, char-literal- and paren-aware
`_split_top_level_args`) and classifies each argument as a pure string
literal or a Java expression (`_literal_content`). The description slot
is positional again — but positional over *arguments*, not over
*string literals* — so a non-literal middle argument no longer shifts
it. A non-literal 3rd argument (e.g. a `DESCR_CONST`) counts as
present-but-unverifiable and produces no finding. Pinned by six new
tests in `tests/test_linting.py` (String.valueOf middle arg, concat
middle arg, non-literal description, dynamic name, comma-in-string,
2-arg missing still fires).

Original entry follows for context.

**Symptom:** `_ARG_SPLIT` assumes the 3rd positional argument in
`model.param().set(name, expr, descr)` is a Java string literal
`"..."`. Java expressions such as `String.valueOf(N_STEPS)` are
treated as "missing description" and fire a benign false-positive
missing-description warning.

**Example offender:**

```java
model.param().set("n_steps", String.valueOf(N_STEPS), "Number of steps");
```

The 3rd argument is the string literal `"Number of steps"` but the
regex sees three non-literal tokens and can't match. Fires
A-missing-description.

**Fix sketch:** extend `_ARG_SPLIT` (or the calling site in
`_scan_missing_descr`) to also accept a trailing comma-separated
string literal after a non-literal 2nd arg, i.e. match the LAST
quoted literal in the `.set(...)` call as the description rather
than strict positional parsing.

**Priority:** Low. Benign false-positive. Does not block builds.
Batch with other linter polish.

---

<!-- New entries append here. -->
