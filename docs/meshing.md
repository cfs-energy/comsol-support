# Meshing through comsol-support — doctrine from a 2,773-domain campaign

Mesh-specific guidance layered on [`mphedit.md`](mphedit.md) (mutator
contract) and [`query-mph.md`](query-mph.md) (read-only probes). Every
rule here was measured, not theorized — the source campaign
(a 13-layer laminate, 2,773 domains, 38,369
boundaries) spent multiple cycles re-deriving each one the hard way.
The compressed symptom-level versions live in
[`known-gotchas.md`](known-gotchas.md) and are auto-suggested on
harness failures; this doc is the connected narrative.

**TL;DR discipline:** measure artifacts, not return statuses; author
sequences create-all-then-move-all and assert the realized order; give
sweeps explicit, validated source faces; set the quality measure at
every read site; run one COMSOL JVM at a time.

## 1. Execution semantics — what `ms.run()` actually commits

Reach the sequence through the component
(`model.component("comp1").mesh("mesh1")` — see
[G-COMP-METHOD-NEEDS-COMPONENT](known-gotchas.md#g-comp-method-needs-component)).
Then:

- **Only whole-sequence `ms.run()` is a reliable commit point.**
  `ms.run("<tag>")` is "build up to tag", not "build only tag" — it
  re-runs the whole upstream sequence, and the tagged feature may or
  may not execute before an aggregate throw
  ([G-MESHRUN-TAG-RERUNS-SEQUENCE](known-gotchas.md#g-meshrun-tag-reruns-sequence)).
- **The throw is cosmetic; the mesh commits.** A failed run can
  persist a large partial mesh — in the source campaign a "failed"
  build was the best mesh of six cycles. Score the artifact, never the
  exception
  ([G-MESH-THROW-COMMITS-PARTIAL](known-gotchas.md#g-mesh-throw-commits-partial)).
- **A failed sequence is poisoned.** Later runs re-throw the *stored*
  aggregate failure in seconds without meshing. Sub-10-second
  "failures" on a big model are stored re-throws — reload the `.mph`
  before judging a fix
  ([G-MESHRUN-STORED-FAILURE-RETHROW](known-gotchas.md#g-meshrun-stored-failure-rethrow)).
- **Saved ≠ re-built.** Adding features and saving does not re-run the
  effector that consumes them; prove the effector re-ran via an output
  invariant that must change
  ([G-MESH-SAVED-FEATURE-NOT-BOUND](known-gotchas.md#g-mesh-saved-feature-not-bound)).

**The census is the ground truth.** After *every* build — thrown or
not — measure per-domain coverage and element totals:

```java
// Which domains hold volume elements (single-arg getElemEntity):
int[] tetDoms  = ms.getElemEntity("tet");
int[] hexDoms  = ms.getElemEntity("hex");
int[] prismDoms = ms.getElemEntity("prism");
int[] pyrDoms  = ms.getElemEntity("pyr");
// unmeshed = all domain ids 1..nDomains minus the union of the above
```

"Success" without a census is not a result — explicit-source sweeps
can emit exactly zero volume elements with `halt_reason: success`
([G-SWEPT-SILENT-ZERO-ELEMENTS](known-gotchas.md#g-swept-silent-zero-elements)).

## 2. Sequence authoring — order is a first-class input

- **Create-all-then-move-all.** `ms.create()` appends after the
  most-recently-*touched* feature, so interleaving creates with moves
  scatters features. The ordering API is `move(String,int)` only
  ([G-MESH-CREATE-AFTER-TOUCHED](known-gotchas.md#g-mesh-create-after-touched)).
- **Assert realized order before running.** Enumerate the sequence and
  compare against the designed order; abort on mismatch. A silent
  misplacement once converted a 20-domain experiment into a 36-domain
  one.
- **The first mesher owns every shared boundary.** Later features must
  conform to its discretisation; one pre-tetted domain converted an
  entire model's sweep from hex to prisms
  ([G-MESH-FIRST-MESHER-OWNS-BOUNDARY](known-gotchas.md#g-mesh-first-mesher-owns-boundary)).
  Order element-type-critical regions first, or use separate mesh
  sequences and accept the non-conforming interface.
- **Never write raw integer entity indices into selections** — derive
  them from geometric rules (`SelectionAlgebra` Box/Cylinder/adjacency)
  so they survive renumbering; a raw-index build in the source campaign
  silently targeted the wrong faces.

## 3. Swept meshing — sourcing, Distribution, and their traps

- `Source face must be specified` means the automatic source/target
  pairing *declined*, not that the domain is unsweepable; the named
  domain moves with ordering. The cure is explicit per-domain source
  faces from a probe-derived map
  ([G-SWEPT-AUTOSOURCE-DECLINE](known-gotchas.md#g-swept-autosource-decline)).
- Validate every explicit assignment: a successful `set` does *not*
  check that the face bounds the domain — assert membership in the
  domain's own `getAdj(3,2)` face list, else the mismatch surfaces
  only at build time
  ([G-SWEPT-SOURCEFACE-READBACK](known-gotchas.md#g-swept-sourceface-readback)).
- `Distribution.numelem` is a **request**: topology and boundary
  conformity outrank it (measured law: realized layers ≥ the domain's
  lateral pre-mesh band count), and the response is non-monotonic —
  more layers can *break* the sweep
  ([G-DISTRIBUTION-NUMELEM-IS-REQUEST](known-gotchas.md#g-distribution-numelem-is-request)).
- Distributions are **scopable child features** with their own dim-3
  selections — several per sweep, partitioning the domains — but a
  Distribution change also perturbs the in-plane discretisation, so
  measure side effects
  ([G-DISTRIBUTION-SCOPABLE-CHILD](known-gotchas.md#g-distribution-scopable-child)).

## 4. Quality measurement — say which number you mean

- **The default measure is `volcircum`, not skewness.** Set
  `MeshStatistics.setQualityMeasure("skewness")` (~3 ms) at *every*
  read site, including inside localisation loops; scoped
  `selection()` reads cost 7–41 ms, so scoping is cheap
  ([G-MESHSTATS-DEFAULT-VOLCIRCUM](known-gotchas.md#g-meshstats-default-volcircum)).
- `MeshSequence.getMinQuality(String)`'s argument is an element
  **type**, not a measure name.
- `getMinVolume` is a **straight-edge tet-decomposition** measure; the
  isoparametric trilinear integral legitimately disagrees on warped
  hexes. State the definition with every claim
  ([G-GETMINVOLUME-STRAIGHT-EDGE](known-gotchas.md#g-getminvolume-straight-edge)).
- Offline element-dump analysis has four convention traps
  (connectivity base, pyramid node order 0-1-3-2, corner-only nodes,
  threshold conventions) — reproduce one COMSOL statistic exactly
  before trusting any novel number
  ([G-MESH-OFFLINE-DUMP-CONVENTIONS](known-gotchas.md#g-mesh-offline-dump-conventions)).

## 5. Probing — read the model's own records first

**Shipped probes.** Three battle-tested probes from the
source campaign ship in `comsol_support/java/probes/` and resolve by bare
name — no authoring needed for the standard reads:

```bash
comsol-support query-mph --input meshed.mph --query MeshStatsProbe
# stats + unmeshed census, CHEAP by default; full sweeps are opt-in:
#   --arg measures=on --arg localize=on   (budget the JVM slot first)
comsol-support query-mph --input meshed.mph --query FeatureProblemProbe
# per-feature build records that survive the aggregate throw
comsol-support query-mph --input meshed.mph --query MeshSelectionProbe
# feature + CHILD selections, sweep sourceface readback, opt-in layer census
```

A path passed to `--query` always wins over the shipped roster, so
campaign-local probes are unaffected. Author additional probes against
the `query-mph` contract ([query-mph.md](query-mph.md)) before mutating
anything:

- **Per-feature build records beat the exception.** Every mesh feature
  retains `buildinfo`/`buildoutput`/`builddetails` describing exactly
  what it produced, and they survive the aggregate throw;
  `hasProblems()`/`problemNames` are not a failure census
  ([G-MESH-BUILDINFO-SURVIVES-THROW](known-gotchas.md#g-mesh-buildinfo-survives-throw)).
- **Enumerate child features too.** `selection()` on children is how
  the scopable-Distribution capability was discovered; feature-level
  enumeration alone misses it.
- **Adjacency conventions:** domain ids are 1-based and `getAdj(3,2)`
  row 0 is the model exterior
  ([G-GETADJ-ROW-ZERO-EXTERIOR](known-gotchas.md#g-getadj-row-zero-exterior));
  validate conventions with the bidirectional round-trip identity over
  the full table; resolve the surface-entity index base pairwise, not
  by absolute facet-area tests
  ([G-ENTBASE-PAIRWISE-RESOLUTION](known-gotchas.md#g-entbase-pairwise-resolution)).
- **Budget probe cost before occupying the JVM slot.** An unscoped
  9-measure × 6-type quality sweep held the (single) COMSOL JVM for
  86 minutes and one leaked JVM survived days at 3 GB RSS. Default
  probes to the cheap scoped reads; make full sweeps opt-in.

## 6. Operational discipline

- **One COMSOL JVM at a time.** Overlapping JVMs contend for license
  seats and scratch, and make native failures unattributable — the
  source campaign's one SIGABRT is permanently unattributed for this
  reason ([G-SIGABRT-NO-HSERR](known-gotchas.md#g-sigabrt-no-hserr)).
  **Tooling:** every harness JVM launch (`edit-mph`,
  `mphgen`, `run-harness`) now takes an advisory per-user slot lock and
  warns in the run log when another run holds it (naming the holder —
  the sidecar stays purely JVM-emitted). Set
  `COMSOL_AGENT_EXCLUSIVE=1` to enforce (wait up to
  `COMSOL_AGENT_SLOT_TIMEOUT` s, default 3600, then fail). Launches
  also warn about pre-existing COMSOL JVMs — a leaked probe JVM once
  survived ~2d19h at 3 GB RSS, possibly holding a seat.
- **Native crash signatures are real mesh outcomes.** Sweeping against
  pre-existing tet volume in thin sheets crashes the native mesher
  ([G-SWEPT-NATIVE-CRASH-PRETET](known-gotchas.md#g-swept-native-crash-pretet));
  a C++ `abort()` can die without an `hs_err` dump, and a negative
  harness exit code is signal death (−6 = SIGABRT, −11 = SIGSEGV).
- **Heap:** `edit-mph` runs at the facade default (`-Xmx48g`); only
  `run-harness` accepts `--jvm-arg`. Plan accordingly for large
  meshes.
- **Timeouts:** `--timeout` is a *kill bound*, not a run duration —
  processes exit early on success; and the calling environment may
  impose its own (different) timeout on top. Record which regime ended
  a run.
- **Heartbeat long builds.** There is no per-step mesh callback (same
  API hole as
  [G-NO-PER-STEP-SOLVER-CALLBACK](known-gotchas.md#g-no-per-step-solver-callback));
  wrap long `mesh().run()` calls with `SolverHeartbeat` so telemetry
  shows liveness.

## 7. Running mesh builds

**Preferred: the first-class verb.** `edit-mph --mesh
<tag>` runs the whole sequence with everything this doc demands built
in — `mesh_heartbeat` liveness with RSS, swallowed-but-reported
failures (partial meshes save; the exception arrives as a `mesh_error`
event with full detail), an automatic post-run `mesh_census`, and
per-feature `mesh_feature_build` records:

```bash
comsol-support edit-mph --input assembly.mph --output assembly_meshed.mph \
    --mutator AddMeshFeatures.java --mesh mesh1 --jvm-arg -Xmx96g \
    --timeout 14400
```

The mutator (optional) authors the sequence; `--mesh` runs it. Check
`mesh_success` in the result and the `mesh_census` in the digest.

**Manual pattern (when the mutator must run the mesh itself, e.g.
staged multi-pass builds).** A mesh build inside a mutator must swallow
the build exception — otherwise the harness aborts and the partial mesh
(often the campaign's real deliverable) is never saved — and must
report what happened through telemetry, since a swallowed exception is
otherwise invisible to the envelope
(`halt_reason: success`, empty `error_detail`):

```java
public static Model mutate(Model m, Map<String,String> args) {
    MeshSequence ms = m.component("comp1").mesh("mesh1");
    // ... create-all-then-move-all, assert realized order ...
    try {
        ms.run();                       // whole-sequence commit point
    } catch (Exception e) {
        // The partial mesh below this throw is real — keep it, and
        // surface the failure through telemetry (harvested into the
        // envelope's error_detail).
        SolverTelemetry.emitError("mesh_error", e);
    }
    // The census is the result — emit it regardless of throw:
    //   per-type getElemEntity() domain lists, unmeshed count, totals.
    return m;   // harness saves; never call save()/disconnect() here
}
```

Score the build afterwards from the telemetry sidecar + census, never
from the exit status alone.

## Symptom → gotcha quick index

| Symptom | Entry |
|---|---|
| Tag-run slow / fails on other features | G-MESHRUN-TAG-RERUNS-SEQUENCE |
| Instant repeat failures after one failure | G-MESHRUN-STORED-FAILURE-RETHROW |
| Throw but mesh looks fine / partial | G-MESH-THROW-COMMITS-PARTIAL |
| Saved new features, mesh unchanged | G-MESH-SAVED-FEATURE-NOT-BOUND |
| Features land in wrong sequence position | G-MESH-CREATE-AFTER-TOUCHED |
| "Source face must be specified" | G-SWEPT-AUTOSOURCE-DECLINE |
| sourceface readback throws/empty | G-SWEPT-SOURCEFACE-READBACK |
| Success but zero volume elements | G-SWEPT-SILENT-ZERO-ELEMENTS |
| numelem ignored / made things worse | G-DISTRIBUTION-NUMELEM-IS-REQUEST |
| Per-region layer counts under one sweep | G-DISTRIBUTION-SCOPABLE-CHILD |
| Meshing A first broke B model-wide | G-MESH-FIRST-MESHER-OWNS-BOUNDARY |
| Quality numbers absurd vs threshold | G-MESHSTATS-DEFAULT-VOLCIRCUM |
| Negative min volume, quadrature disagrees | G-GETMINVOLUME-STRAIGHT-EDGE |
| Can't tell which feature failed | G-MESH-BUILDINFO-SURVIVES-THROW |
| Offline dump finds phantom folds | G-MESH-OFFLINE-DUMP-CONVENTIONS |
| Adjacency off-by-one / 285-face "domain" | G-GETADJ-ROW-ZERO-EXTERIOR |
| Which surface index base? | G-ENTBASE-PAIRWISE-RESOLUTION |
| Native crash during sweep | G-SWEPT-NATIVE-CRASH-PRETET |
| exit=-6, no dump, no envelope | G-SIGABRT-NO-HSERR |
