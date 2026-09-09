# Known gotchas — COMSOL 6.4 + headless solve

Living document. Each entry: **symptom** (search index), **root cause**,
**workaround**, **observed in**.

New campaigns append on discovery. Entries are queryable via
`comsol-support gotcha-search "<keyword>"`.

The schema is deliberately flat (one paragraph per gotcha, headings as
ID anchors) so the search CLI can grep without parsing a tree.

---

## G-DISCONNECT-HANGS

**Symptom**: `ModelUtil.disconnect()` never returns. JVM thread-dump
shows the main thread parked indefinitely.

**Root cause**: In headless mode, `ModelUtil.disconnect()` waits on a
license-release ack that does not arrive when running standalone (vs.
client-server). This is by design in the COMSOL API but not flagged
loudly in vendor docs.

**Workaround**: Skip `ModelUtil.disconnect()`. Exit the JVM with
`System.exit(N)` directly — process teardown frees license + resources
cleanly. This is what `ModelExporter.java` does (see comment at
`finally { … }`).

**Observed in**: COMSOL 6.4, all subreleases through 6.4.0.293.

---

## G-LIBPATH-CAD-IMPORT

**Symptom**: Loading a `.mph` that contains imported CAD geometry
fails with `UnsatisfiedLinkError` or a load_error halt event whose
message mentions `libcadimport.so` (or platform equivalent).

**Root cause**: COMSOL's CAD-import shared library lives under
`{comsol}/ext/cadimport/glnxa64/` (Linux), not the base `lib/glnxa64/`.
`LD_LIBRARY_PATH` must include the glob `{comsol}/ext/*/glnxa64` to
cover this and similar plugin ext libraries (`graphicsmagick`,
`acis`, …).

**Workaround**: Use `JavaFacade.get_comsol_env()`, which sets
`LD_LIBRARY_PATH` to `lib/{plat}` + `lib/{plat}/ext` + the
`ext/*/glnxa64` glob. If authoring a bespoke harness outside the
facade, replicate the same env construction.

**Observed in**: COMSOL 6.4 with imported STEP/IGES geometry from
applications/.

---

## G-CONSISTENT-OFF-TOKEN

**Symptom**: Setting `consistent` to `off` on a study step has no
visible effect — the solver behaves as if the flag were on.

**Root cause**: COMSOL canonicalizes the property to either the
literal string `"off"` (lowercase, unquoted in the Java call) or a
Java boolean — but the GUI sometimes writes the string `"Off"`
(capital). Mixing capitalizations or quoting style produces a silent
no-op.

**Workaround**: Set with the canonical lowercase string:
```java
model.study("std1").feature("stat").set("consistent", "off");
```
Verify with `model.study("std1").feature("stat").getString("consistent")`
— it should round-trip as `"off"` exactly.

**Observed in**: COMSOL 6.4 on stationary studies with custom
parametric continuation.

---

## G-PROPERTY-KEY-NOT-RECOGNIZED

**Symptom**: `model.feature("X").set("propertyName", value)` raises
`com.comsol.util.exceptions.FlException: Property not recognized:
propertyName`.

**Root cause**: COMSOL property keys are NOT the GUI labels — they are
internal API names that often differ. The Javadoc lists method
signatures but not the property-key set; the Reference Manual PDF
lists property tables but is hard to search. Free-form synthesis of
property keys produces about a 30% miss rate.

**Workaround**: Two paths.
1. **Best**: query the `knowledge` table (populated by
   `refmanual_scraper`) before writing the Java:
   `comsol-support search "<feature-name>" --stage physics`.
2. **Fast**: open the feature in the COMSOL GUI's MATLAB/Java view
   ("Application Builder" → "Record code") to see the canonical key
   COMSOL itself emits.

**Observed in**: COMSOL 6.4 across every feature class. Highest
density in `mfh`, `ec`, `ht`. Often paired with a GUI label that LOOKS
identical to what was written (capitalization or pluralization drift).

---

## G-INTEGRATION-COUPLING-ARGUNITS

**Symptom**: After mutating an integration-coupling operator's
expression argument (`intop1.set("expr", "newexpr")`), the deduced unit
of `intop1(varname)` does not match the new expression's units. Layer C
flags it as inconsistent.

**Root cause**: Integration-coupling operators cache argument-unit
metadata that does NOT auto-recompute on `.set("expr", …)`. The
`argunits` property must be re-declared explicitly.

**Workaround**: After mutating the integrand expression, also call:
```java
model.cpl("intop1").set("argunits", "<unit-of-new-expression>");
```
Or restate it as part of the same mutator method.

**Observed in**: COMSOL 6.4, mutators that swap the integrand of
existing nonlocal-coupling operators across cycles.

---

## G-PROBE-GETREAL-MISSING

**Symptom**: `model.probe(tag).getReal()` raises
`NoSuchMethodException` on some 6.4 builds when accessed via the typed
interface.

**Root cause**: `getReal()` is not on the canonical
`ProbeFeature` typed interface on every COMSOL 6.4 subrelease — it is
present at runtime via reflection but absent from the compile-time
type.

**Workaround**: Reflect:
```java
Object probe = model.probe(tag);
double[][] data = (double[][]) probe.getClass()
        .getMethod("getReal").invoke(probe);
```
This is what `ModelExporter.extractResults()` does (see comment around
`Reflection: ProbeFeature.getReal()`).

**Observed in**: COMSOL 6.4.0.293 build (internal harness, 2026-04-30).

---

## G-NO-PER-STEP-SOLVER-CALLBACK

**Symptom**: Looking for a `study.run()` hook that fires per Newton /
time / parametric step. Cannot find one.

**Root cause**: COMSOL 6.4's public Java API does not expose a per-step
solver callback. Confirmed-negative dead end after multi-cycle
reflective probing. See [`docs/solver-progress.md`](solver-progress.md)
for the full investigation log.

**Workaround**: Use `SolverHeartbeat.java` to emit a "still alive"
telemetry event at a fixed cadence (2 s default) while `study.run()`
is in flight. For deeper detail, use JVM thread-dump polling (also
documented in `solver-progress.md`).

**Observed in**: COMSOL 6.4, all releases probed.

---

## G-INITSTANDALONE-EXIT-CODE

**Symptom**: Java subprocess that calls `ModelUtil.initStandalone(...)`
exits 0 even on clear failures (no .mph saved, no telemetry).

**Root cause**: `System.exit(0)` is implicit in the JVM default
shutdown hook chain; uncaught exceptions in the main method still
exit cleanly unless code explicitly sets a non-zero status.

**Workaround**: Track a `haltReason` variable in `main`, and in the
`finally` block call `System.exit("success".equals(haltReason) ? 0 : 1)`.
Pattern is in `ModelExporter.main`.

**Observed in**: All COMSOL 6.x JVM harnesses we have written;
fundamental to JVM semantics, not COMSOL-specific.

---

## G-MPH-PARTIAL-SAVE-NOISE

**Symptom**: Running `comsol-support check` on a `.partial.mph` produced
by a failed solve emits dozens of unit / description warnings that look
like real defects but are artifacts of the mid-edit save.

**Root cause**: `ModelExporter` writes `*.partial.mph` on failure paths
to preserve diagnostic state. The model graph at that point may have
half-applied physics, missing solver sequences, or stale references.

**Workaround**: `comsol-support check` now classifies `.partial.mph`
inputs and skips by default with a clear message. Pass `--allow-partial`
to lint anyway. See [`docs/linting.md`](linting.md) → Partial-state
handling.

**Observed in**: Internal benchmark campaigns (multiple).

---

## G-COMP-METHOD-NEEDS-COMPONENT

**Symptom**: `model.geom("geom1")` works but `model.physics("ht")`
raises `FlException: Tag not found` on a freshly-loaded `.mph`.

**Root cause**: Several feature collections are component-scoped, not
model-scoped: `physics`, `material`, `variable`, `mesh`. The model-
level method is a legacy alias that works only if there is exactly
one component.

**Workaround**: Always reach through `component("comp1")`:
```java
model.component("comp1").physics("ht");
model.component("comp1").material("mat1");
model.component("comp1").variable("var1");
model.component("comp1").mesh("mesh1");
```

**Observed in**: COMSOL 6.4 on multi-component models, especially
inherited models from corpus mining.

---

## G-SLF4J-OSGI-STANDALONE

**Symptom**: `ModelUtil.load()` on a model that uses the Heat Transfer
module fails with `Failed_to_initialize_physics_interface`. With the
full stack trace exposed, the failure is in
`org.slf4j.impl.SLF4JLoggerFactory.getSLF4JLoggerFactory`, triggered
during `com.comsol.heat.util.ashrae.l.<clinit>` (the Heat Transfer
ASHRAE ambient-properties database, which opens a SQLite connection
via JDBC, which needs SLF4J).

**Root cause**: COMSOL's `plugins/` directory ships
`org.osgi.slf4j.osgi-*.jar` — an OSGi-specific SLF4J binding whose
`StaticLoggerBinder.<clinit>` requires an OSGi class-loader to
initialize. In a flat-classpath standalone Java app (which
`ModelUtil.initStandalone()` runs as), this binding fails. The first
binding found on the classpath wins, and the OSGi binding is what the
classloader picks up.

**Workaround**: Prepend `bin/tomcat/lib/org.slf4j.slf4j-jdk14-*.jar`
(a plain JDK14-backed SLF4J binding shipped alongside the bundled
Tomcat web layer) to the classpath, ahead of the plugins directory.
This is what `comsol_support.java_facade.JavaFacade.get_full_classpath`
now does automatically. The invariant is pinned in
`tests/test_lint_selftest.py::test_standalone_slf4j_binding_precedes_osgi_binding`.

**Observed in**: COMSOL 6.4. Any standalone load of a model using the
Heat Transfer module's ambient-properties feature (most multiphysics
thermal models). Was a hard blocker for headless audit of a multiphysics
thermal model; the fix is the only reason that audit could
load the model headlessly at all.

---

## G-VARIABLE-EVALUATEUNIT-MISSING

**Symptom**: `model.variable("var1").evaluateUnit("v")` raises
`NoSuchMethodException`. Tried as a way to extend Layer B to per-
variable unit checks.

**Root cause**: `evaluateUnit(name)` is exposed on `ParamBase` (used
by `model.param()`) but NOT on `VariableFeature.varnames()` — variable
units are not introspectable via the typed API.

**Workaround**: For variables, either (a) rely on Layer C symbolic
analysis seeded via the `variable_declared_units` Source-B table, or
(b) declare the unit explicitly via the COMSOL-side `.set(name,
expr, descr)` and rely on the description scan to verify presence.
Direct runtime introspection is not available.

**Observed in**: COMSOL 6.4 (confirmed-negative for Layer B
enhancement).

<!-- New entries append below. ID convention: G-DOMAIN-PHENOMENON. Keep
     to ≤200 words each. Cross-link with [G-X](#g-x). -->

---

## G-SELECTION-WIPE-ON-GEOM-REIMPORT

**Symptom**: After replacing a geometry's *feature sequence* (e.g.
swapping the native build for a single flat `Import`, or re-importing
identical geometry), the model silently breaks: coupling operators
evaluate over empty sets (`aveop`/`intop` return 0 or NaN), physics
features report undefined variables, and geometry-built named
selections vanish — even though the imported entity numbering is
byte-for-byte identical.

**Root cause**: Selections are bound to the geometry *sequence* that
produced them, not to entity numbers. Replacing the sequence clears
every selection that referenced it — coupling-operator selections,
physics-feature selections, and geometry-built named selections alike.
Geometry-built named selections in particular cannot be reconstructed
from a flat import, because the import carries no record of the
build-time selection criteria.

**Workaround**: Prefer preserving the *native* geometry feature
sequence rather than re-importing. If a re-import is unavoidable, plan
to rebuild the entire selection web afterwards (re-create every
coupling operator, physics, and named selection against the new
sequence) and accept that geometry-built named selections must be
re-authored by hand. When diagnosing, `query-mph` with a probe that
lists coupling-operator selection membership and per-feature selection
sizes surfaces the wipe immediately.

**Observed in**: COMSOL 6.4 — model-authoring lesson, geometry-
sequence replacement on a multi-component thermal model.

---

## G-NO-JSTACK-USE-SIGQUIT

**Symptom**: A headless solve appears hung and you want a Java
thread-dump, but `jstack` is not present — the COMSOL-bundled JRE
ships no `jstack`/`jcmd` (only `java`/`javac`).

**Root cause**: COMSOL bundles a trimmed JRE under
`{comsol}/java/{plat}/jre/bin` without the JDK diagnostic tools.

**Workaround**: Send `SIGQUIT` to the JVM — `kill -3 <pid>` — and the
JVM prints a full thread dump to its **stdout**. When the JVM was
launched via `comsol-support run-harness` (or any `JavaFacade.run_class`
caller), that stdout is teed verbatim to the run log, so the dump is
captured even though the console view may be filtered. Find the PID
with `pgrep -f <ClassName>` or from the run log.

**Observed in**: COMSOL 6.4 bundled JRE, glnxa64.

---

## G-MESHRUN-TAG-RERUNS-SEQUENCE

**Symptom**: `mesh("mesh1").run("<tag>")` intended to build only the tagged
feature takes as long as a full mesh build, and failures name *other*
features. Conversely, an appended feature sometimes commits work before the
run throws.

**Root cause**: `ms.run(tag)` re-runs the whole upstream sequence up to and
including the tag — it is "build up to tag", not "build only tag". A
throwing upstream feature aborts the aggregate run; the tagged feature may
or may not have executed by then (both outcomes observed: one build
committed 14 tet domains via `run("ft1")` before the aggregate throw;
others never reached the appended feature).

**Workaround**: Treat only whole-sequence `ms.run()` as a reliable commit
point. After any tag-run, verify by output invariant (per-domain element
census), never by "the call returned". See
[G-MESH-THROW-COMMITS-PARTIAL](#g-mesh-throw-commits-partial) and
[G-MESHRUN-STORED-FAILURE-RETHROW](#g-meshrun-stored-failure-rethrow).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESHRUN-STORED-FAILURE-RETHROW

**Symptom**: After one failed mesh build, every later `ms.run(tag)` fails
in seconds (1.6–5 s vs ~240 s for a real build) with
`Multiple_problems_occurred_when_building_X#Mesh 1`, regardless of what
changed in between.

**Root cause**: The sequence stores the first failure as a problem record;
subsequent runs re-aggregate and re-throw the stored exception without
attempting to mesh. The fast re-throw is the tell: no meshing happened.
Reading the stored message as a fresh per-feature failure produces false
mechanism narratives.

**Workaround**: Time the run. Sub-10-second "failures" on a large model
are stored re-throws — reload the `.mph` to clear the problem state before
judging whether a fix worked. Harvest real per-feature outcomes from
feature build records
([G-MESH-BUILDINFO-SURVIVES-THROW](#g-mesh-buildinfo-survives-throw)).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESH-THROW-COMMITS-PARTIAL

**Symptom**: `ms.run()` throws, so the build is scored as failed — yet the
saved model contains a large, usable partial mesh (in one campaign the
"failed" build was the best mesh of six cycles).

**Root cause**: The whole-sequence run commits per-feature work as it
goes; the terminal exception is an aggregate report, not a rollback. One
build threw at 199 s and persisted 2,736/2,773 swept domains.

**Workaround**: Never gate on the exception. After every build — thrown or
not — measure: `getElemEntity` census per domain, unmeshed-domain count,
element totals by type. Score the artifact, not the exception. Mesh
mutators must catch the exception so the harness still saves the partial
mesh (and should emit the error as telemetry — see `docs/meshing.md`).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESH-SAVED-FEATURE-NOT-BOUND

**Symptom**: A mutator adds mesh features (e.g. `Distribution` children),
saves cleanly, telemetry shows success — but the output mesh is
byte-for-byte the old baseline.

**Root cause**: Adding and saving features does not re-run the effector
that consumes them. If the sweep never re-ran, the mesh stored in the
input model is what gets saved to the output.

**Workaround**: Prove the effector re-ran via an output invariant that
must change (hex count, element census) and emit it as a telemetry event
(e.g. `swe1_reran`). "Saved" and "re-built" are different states; a build
is only scored after the invariant moves.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESH-CREATE-AFTER-TOUCHED

**Symptom**: `ms.create(tag, op)` inserts the new feature in the "wrong"
place: sometimes at the end, sometimes right after a feature you last
moved. Order-sensitive builds behave differently than designed.

**Root cause**: `create()` appends after the most-recently-*touched*
feature, not at sequence end — any prior `move(...)` changes where the
next `create` lands. Also an API-surface fact: ordering is
`move(String,int)` only; `moveUp`/`moveDown`/`moveToFirst`/`moveToLast`
do not exist on `MeshFeatureListClient`.

**Workaround**: Create-all-then-move-all; never interleave creates with
moves. Then assert the realized sequence order equals the designed order
before `ms.run()` — a silent misplacement once converted a 20-domain test
into a 36-domain one.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-SWEPT-AUTOSOURCE-DECLINE

**Symptom**: Swept mesh fails with `Failed to create swept mesh for
domain. - Domain: N. Source face must be specified.` on a domain that is
geometrically sweepable.

**Root cause**: The error means COMSOL's automatic source/destination cap
pairing *declined* (ambiguous candidates) — not that the domain is
non-sweepable. The domain the error names moves with mesh ordering (2 → 1
after pre-tetting {2}); it reflects auto-pairing state, not a defect of
the named domain. Error classes partition by sourcing mode: auto-source
builds fail with "Source face must be specified"; explicit-source builds
fail with "Inverted element" — disjoint sets across an entire campaign
corpus.

**Workaround**: Assign explicit per-domain source faces (staged sweep)
from a probe-derived `{domain, srcFace, dstFace}` map; validate each
assignment per
[G-SWEPT-SOURCEFACE-READBACK](#g-swept-sourceface-readback).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-SWEPT-SOURCEFACE-READBACK

**Symptom**: On an auto-source `Swept`, `getString("sourceface")` throws
(`Property 'sourceface' cannot be converted to string`) and
`selection("sourceface")` resolves but reports zero entities at every
dimension — looks like a broken API.

**Root cause**: Readback semantics differ by sourcing mode. Auto-source:
the chosen faces are not exposed through the property. Explicit
assignment: `selection("sourceface")` reads back correctly at assignment
time. Worse, a successful `set` does NOT validate that the face actually
bounds the domain — offset src/dst pairs were accepted silently and
failed only at build time.

**Workaround**: For explicit sourcing, assert membership at assignment
time: the assigned face must appear in the domain's own `getAdj(3,2)`
face list, else abort. Never write raw integer entity indices into
selections ([G-GETADJ-ROW-ZERO-EXTERIOR](#g-getadj-row-zero-exterior)).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-SWEPT-SILENT-ZERO-ELEMENTS

**Symptom**: An explicit-source staged `Swept` build completes with
`halt_reason: success` and no exception — and produced exactly zero
volume elements.

**Root cause**: Two distinct silent-zero signatures exist: (a) surface
output only (a `buildoutput` record with no volume elements); (b) no
`buildoutput` record at all. Class (b) is position-mediated: the same
feature emits output when moved earlier in the sequence.

**Workaround**: A post-build element census is mandatory
([G-MESH-THROW-COMMITS-PARTIAL](#g-mesh-throw-commits-partial)) —
"success" without a census is not a result. Harvest per-feature
`buildoutput` to distinguish (a) from (b), and test sequence position for
class (b).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-DISTRIBUTION-NUMELEM-IS-REQUEST

**Symptom**: A swept `Distribution` with `numelem=N` yields domains with
far fewer layers than N, domains that ignore every value, and refinement
that makes closure *worse*.

**Root cause**: `numelem` is a request the sweeper honours only where
source/target topology permits — not a set-to-N and not a multiplier
(measured: 1→2 when asked for 13; 22-layer columns unchanged at both 1
and 13). Boundary conformity outranks it: on 2,736/2,736 pure-swept
domains, realized `layers(d) ≥ band_count_max(d)` (equality on 2,447) —
pre-meshed lateral imprints set a floor the Distribution cannot undercut.
The response is also non-monotonic (residual 21 @ numelem 1, 18 @ 13,
119 @ 33 — at 33 the sweep itself starts failing).

**Workaround**: Treat layer counts as emergent. Measure realized layers
per domain; sweep `numelem` empirically and expect a knee, not a
monotone improvement.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-DISTRIBUTION-SCOPABLE-CHILD

**Symptom**: A global sweep needs different through-thickness counts in
different regions; per-domain `Sweep` features seem required and fight
each other.

**Root cause**: Undocumented capability plus a side effect. `Distribution`
is a *child feature* of the sweep and carries its own dim-3 selection —
it is scopable per-domain even when the parent `Swept` is `isRemaining`.
Discovered only by calling `selection()` on child features. Caveat: a
`Distribution` change also perturbs the in-plane/source-face
discretisation (quads 157,983 → 438,017 at numelem 1→13; mechanism
unestablished), so scoping is not side-effect-free.

**Workaround**: Partition domains across multiple `Distribution` children
under one sweep and assert the selections partition exactly (their union
and pairwise disjointness), then measure in-plane effects rather than
assuming isolation.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESH-FIRST-MESHER-OWNS-BOUNDARY

**Symptom**: Meshing region A first suddenly degrades region B model-wide:
hexes collapse to prisms everywhere, or previously-meshing domains fail.

**Root cause**: Whichever mesh feature runs first owns the shared
boundary's discretisation; every later feature must conform. Measured:
pre-tetting ONE ambient domain handed triangulated caps to ~200
neighbours and converted the entire model's sweep to prisms (149,118 hex
→ 0; 1,065,098 prisms). The same 20 thin sheets that yield 0 tets in
append order yield 223,351 tets in 36 s when run first.

**Workaround**: Order the sequence so element-type-critical regions mesh
first. When two regions contend for a shared face, decide ownership
deliberately — or split into separate mesh sequences and accept a
non-conforming interface.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESHSTATS-DEFAULT-VOLCIRCUM

**Symptom**: Mesh quality numbers look catastrophically low against a
skewness threshold, or "change" when probed two ways.

**Root cause**: The default quality measure is `volcircum`, NOT skewness —
every `getMinQuality()`/`getQualityDistr()` read without a prior
`setQualityMeasure(...)` reports volcircum. Two adjacent traps:
`MeshSequence.getMinQuality(String)`'s argument is an element TYPE
(`"hex"`), not a measure — a measure name throws
`Unknown_mesh_element_type`; and a localisation loop that restores the
default measure mid-loop silently reverts to volcircum (a campaign probe
did this and mislabelled 41 domains for several cycles).

**Workaround**: Set the measure explicitly at every read site —
`setQualityMeasure("skewness")` costs ~3 ms, scoped
`MeshStatistics.selection()` reads cost 7–41 ms — and record which
measure every published number used.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-GETMINVOLUME-STRAIGHT-EDGE

**Symptom**: `MeshStatistics.getMinVolume("hex")` reports a negative
volume ("inverted element!") but exact isoparametric quadrature of the
same hex is positive everywhere.

**Root cause**: Two valid definitions that disagree on warped hexes.
COMSOL's volume statistic is a straight-edge tetrahedral-decomposition
measure (a 6-tet Freudenthal split reproduces COMSOL's −3.399e-9 to five
digits); the trilinear isoparametric integral of the same hex is
legitimately positive (+5.557e-14). Totals and maxima reconcile because
warped-face terms cancel across shared faces — only the *min* exposes the
divergence. Neither number is wrong.

**Workaround**: State the definition alongside every volume/quality
claim. Use `getMinVolume` as a straight-edge inversion detector; use
interior-sampled signed Jacobians for isoparametric validity
([G-MESH-OFFLINE-DUMP-CONVENTIONS](#g-mesh-offline-dump-conventions)).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-MESH-BUILDINFO-SURVIVES-THROW

**Symptom**: After a failed mesh build you cannot tell which features
actually produced elements: the aggregate exception names one domain,
`mesh.hasProblems()` flags features that *committed* elements, and
`problemNames` returns null.

**Root cause**: The aggregate throw hides per-feature outcomes, and the
problem-flag API is not a failure census. But every mesh feature retains
`buildinfo`/`buildoutput`/`builddetails` properties recording exactly
what it produced (e.g. "Triangles, 94 … no tetrahedra") — and these
survive the exception.

**Workaround**: Harvest per-feature build records after every build,
failed or not, and base the failure census on them — never on
`hasProblems()` or the exception text.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign (`FeatureProblemProbe`).

---

## G-MESH-OFFLINE-DUMP-CONVENTIONS

**Symptom**: Offline analysis of exported element dumps "finds" thousands
of folded elements COMSOL doesn't report, or cannot reproduce COMSOL's
counts.

**Root cause**: Four measured convention traps: (1) connectivity index
base must be determined ONCE from the union over all element types — a
per-type heuristic shifted every prism and fabricated 11,549 "folded"
prisms; (2) pyramid base ordering in the dump is 0-1-3-2, not 0-1-2-3;
(3) dumps carry corner nodes only, so offline quality is an upper bound
if COMSOL uses curved element geometry; (4) corner-only Jacobian checks
are insufficient for trilinear hexes (det J can dip negative in the
interior), and threshold counts like min-Jac/diam³ are
convention-dependent by ~30× (reference element, sample grid, diameter
definition).

**Workaround**: Pin all four conventions explicitly beside every offline
number, and validate the pipeline by reproducing one COMSOL statistic
exactly before trusting any novel one.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-GETADJ-ROW-ZERO-EXTERIOR

**Symptom**: Off-by-one chaos in adjacency analysis — a "domain" with 285
faces appears, or the last domain is missing from a walk.

**Root cause**: Domain entity ids are 1-based and `getAdj(3,2)`'s table
has nDomains+1 rows: row 0 is the model EXTERIOR. A `for d in [0,
nDomains)` walk scores the exterior as a domain and drops the last one.

**Workaround**: Iterate ids 1..nDomains with row index = id. Validate any
adjacency convention with the bidirectional round-trip identity —
`f ∈ getAdj(hi,lo)[row(h)] ⟺ h ∈ getAdj(lo,hi)[row(l)]` — over the FULL
table (a forward-only sample covered only 10.9 % of incidences; a
deliberately injected offset trips the full test with ~290k mismatches).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-ENTBASE-PAIRWISE-RESOLUTION

**Symptom**: You need the surface-entity index base (0 vs 1) for offline
face analysis, and an absolute facet-area cross-check refuses to certify
either base.

**Root cause**: Curvature bias defeats absolute tests — a curved face is
under-measured by its own flat facets, so even the CORRECT base misses a
tight tolerance (the right base agreed on only 1/24 faces in the strict
test).

**Workaround**: Compare the bases pairwise: curvature penalises both
equally, while a one-index shift is wrong by order unity. Exclude
non-discriminating pairs (near-equal neighbouring faces), require
unanimity on the discriminating subset plus an order-of-magnitude median
separation. Measured result on one model family: entbase = 1, unanimous
across three meshes.

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-SWEPT-NATIVE-CRASH-PRETET

**Symptom**: The JVM dies with a native crash during a swept build
(SIGSEGV/SIGABRT; when a dump exists it shows `vector::_M_range_insert`
in the mesher).

**Root cause**: Sweeping against pre-existing tetrahedral volume in thin
laminate sheets crashes COMSOL's native mesher — reproduced across three
independent builds (tet-first orderings). It is specific to thin-sheet
pre-tet: pre-tetting a bulky domain produced a clean topological
complaint instead of a crash.

**Workaround**: Do not order tet passes over thin sheets before a sweep
that must conform to them; prefer explicit source faces or a separate
mesh sequence for the sheets. Expect that a dump may not exist —
[G-SIGABRT-NO-HSERR](#g-sigabrt-no-hserr).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign.

---

## G-SIGABRT-NO-HSERR

**Symptom**: `edit-mph`/`run-harness` reports "produced no JSON envelope.
exit=-6" (or another negative code); no output `.mph`, no halt event —
and NO `hs_err_pid*.log` anywhere.

**Root cause**: A C++ `abort()` inside COMSOL's native mesher/solver can
kill the process without reaching the JVM crash handler, so no hs_err
file is written. A negative harness exit code is signal death (−6 =
SIGABRT, −11 = SIGSEGV). Absence of a dump is NOT absence of a native
crash. If multiple COMSOL JVMs overlap, the failure becomes
unattributable (and the JVMs contend for license seats and scratch).

**Workaround**: Run ONE COMSOL JVM at a time and stamp runs in telemetry.
When a dump does exist it lands in the JVM's cwd — collect it beside the
build artifacts. Treat mid-heartbeat telemetry silence plus a negative
exit as native death; for hang-vs-crash diagnosis see
[G-NO-JSTACK-USE-SIGQUIT](#g-no-jstack-use-sigquit).

**Observed in**: COMSOL 6.4, a 2,773-domain laminate meshing campaign
(its one SIGABRT remains unattributed because two JVMs overlapped).
