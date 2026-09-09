# COMSOL modeling practice — stage-by-stage guide

Curated modeling guidance for an agent (or human) building COMSOL models
through comsol-support's deterministic floor (`mphgen`, `edit-mph`,
`query-mph`, `run-harness`, `check`). It is organized by the natural build
order and, for each stage, records the **mindset**, the **invariants** a
valid model must satisfy, the **pitfalls** that most often produce models
that compile but solve wrongly, and an **API quick-ref**.

Provenance: harvested 2026-08 from the retired build-orchestrator stage
preambles (`stages/*.xml`, `_protocol.xml`) and the native-preference
rules, when the orchestrator (L5) was deprecated. Every API name below was
checked against the COMSOL 6.4 Programming Reference / Reference Manual on
disk; names the original preambles had wrong are corrected and noted, and
anything unverifiable is marked ⚠ rather than asserted.

How this doc relates to its neighbours:

| Doc | Role |
|---|---|
| [`known-gotchas.md`](known-gotchas.md) | symptom-indexed, *measured* pitfalls with root cause + workaround; auto-suggested on harness failures |
| [`meshing.md`](meshing.md) | campaign-measured mesh doctrine (commit semantics, census, swept sourcing) — the Mesh section below defers to it |
| [`linting.md`](linting.md) | the unit/description rules that are *enforced* (Layers A/B/C) |
| [`facade-conventions.md`](facade-conventions.md) | TagRegistry / SelectionAlgebra conventions referenced by the invariants |
| MCP tools `list_physics_options`, `list_studies_for_physics`, `list_default_plots_for_physics`, `search_api`, `search_fragments` | the retrieval side of the native-first ladders and the verification ladder |

---

## Working discipline

**Never guess physics — escalate.** When confidence is low (no close
fragment or exemplar, or the construction diverges from what the corpus
shows), stop and present the user exactly two concrete options: (A) the
closest available approach with its trade-offs stated; (B) an alternative
approach, or a request for clarification. Silent guessing in COMSOL
produces models that *compile but solve incorrectly* — the worst failure
mode, because nothing flags it.

**Dry-run before any solve.** Compile and serialize the model (an unsolved
`.mph` via `mphgen`, or `edit-mph --no-save` / `query-mph` probes) before
attempting a solve. Structural errors surface in seconds at that point;
they surface as opaque solver failures minutes later otherwise.

**The verification ladder** (replaces the old "Tier A/B/C" declarations —
the confidence idea is kept, the comment protocol is retired):

1. **Compose from a known-good fragment** when one exists (`search_fragments`,
   `comsol-support search`); parameter substitution only.
2. **Adapt a corpus exemplar** found by similarity; structural changes are
   checked against the ontology.
3. **Free synthesis** only when neither fits — and then verify *every*
   `.create(_, "<Type>")` type name and every `.set("<key>", _)` property
   key against the ontology (`comsol-support search "<Type>"`,
   `search_api`) before running. Property keys are the #1 synthesis miss
   ([G-PROPERTY-KEY-NOT-RECOGNIZED](known-gotchas.md#g-property-key-not-recognized)).

**Score artifacts, not return statuses.** A thrown build can leave a
usable partial model and a "successful" run can leave nothing — measure
(census, probes, sidecars). See `meshing.md` §1 and
[G-MESH-THROW-COMMITS-PARTIAL](known-gotchas.md#g-mesh-throw-commits-partial).

---

## Native-first ladders

Reach for COMSOL's native constructs before custom ones. Each ladder names
the anti-pattern, the fallback order, and the MCP tool that lists the
native options for the model at hand.

### Physics — tool: `list_physics_options`
**Anti-pattern:** reaching for `GeneralFormPDE` / `CoefficientFormPDE` /
`WeakFormPDE` as the *first* move when the intent names a standard physics
domain. WHY: in those domains the native interface almost always exists,
is shorter, and comes with preset plots and probes for free.
1. Add a **`WeakContribution`** feature *inside* the native interface when
   custom terms are needed — keeps default plots, BCs, and solver tuning
   intact: `model.component("comp1").physics("ht").create("weak1", "WeakContribution", 3)`.
2. Couple two native interfaces via **Multiphysics**.
3. Fall through to the PDE interfaces only when the above do not fit. Most
   "I need custom physics" situations resolve at step 1.

### Studies — tool: `list_studies_for_physics`
**Anti-pattern:** hand-building a `SolverSequence` feature by feature when
a preset step gives the same result in two lines. WHY: preset steps carry
physics-specific solver tuning that is nontrivial to reproduce correctly.
1. Use the preset step matching the physics (`Stationary`, `Transient`,
   `Frequency`, `Eigenfrequency`). Defaults are typically correct.
2. Override only the specific numeric setting you need (e.g. a relative
   tolerance) and note the deviation.
3. Build a manual solver sequence only when the brief requires unusual
   solver behaviour.

### Postprocessing — tool: `list_default_plots_for_physics`
**Anti-pattern:** creating custom `Dataset` + `PlotGroup` combinations when
a preset plot displays the same field with physics-supplied expressions
already bound. WHY: the default plot groups that ship with each physics
interface handle unit conversion, iso-value auto-ranging, and legend
formatting without hand-configuration.
1. Use a preset plot type (`Surface`, `Volume`, `Line`, `Global`) with the
   physics-supplied expression (`T`, `spf.U`, `solid.disp`).
2. Use a preset **Probe** (domain / boundary / point) when the brief asks
   for a scalar answer — probes auto-evaluate during the solve and appear
   in the `result_probe` telemetry stream.
3. Build custom datasets only when the preset types cannot express the
   required output.

---

## Stage 0 — Intent

**Mindset.** Decompose the request into COMSOL-native concepts: which
physics modules (heat transfer, solid mechanics, CFD, electromagnetics,
acoustics, chemical transport…) and whether coupling is required (Joule
heating = Electric Currents + Heat Transfer). Assess geometric complexity,
study types (stationary, transient, frequency domain, eigenfrequency), and
required outputs. When the request is ambiguous, ask — do not guess physics.

**Invariants.**
1. Produce a structured intent: physics modules, geometry description,
   boundary-condition summary, study type, expected outputs (derived
   values, plots, exports).
2. Identify ALL coupled physics before proceeding — discovering a missing
   module at the physics stage forces a restart from the beginning.
3. Estimate a complexity tier (simple / medium / complex) to guide mesh
   density and solver choices downstream.
4. Confirm with the user before proceeding if the request implies more
   than two coupled physics interfaces.

**Pitfalls.**
- Missing coupled physics: thermal expansion needs Heat Transfer *and*
  Solid Mechanics with thermal strain enabled; Joule heating needs Electric
  Currents coupled to Heat Transfer; Marangoni flow needs Laminar Flow
  coupled to Heat Transfer with a surface-tension gradient.
- Wrong study type: Stationary for time-varying boundary conditions;
  Transient where Frequency Domain would be far cheaper for harmonic
  excitation.
- Over-specifying geometry here — intent captures topology and approximate
  dimensions, not exact coordinates.
- Not asking when critical information is missing ("simulate a heat sink"
  — what material, what heat load, what cooling conditions?).

**API quick-ref.** Planning stage — no COMSOL API calls.

---

## Stage 1 — Parameters

**Mindset.** Every numeric value that might change is a named parameter:
geometric dimensions, non-library material properties, operating
conditions, loads, sweep ranges. Parameters make the model reusable and
enable parametric studies without editing code. Group them logically
(geometry, material, operating conditions).

**Invariants.**
1. Every parameter has a name, an expression *with units*, and a
   description.
2. Names are valid COMSOL identifiers: start with a letter; letters,
   digits, underscores only; no spaces.
3. Use SI units consistently unless the domain has a strong convention
   otherwise (e.g. eV in semiconductor physics).
4. Track parameter entries through TagRegistry (prefix `par`) when using
   the facade — see `facade-conventions.md`.
5. Never use reserved COMSOL variable names as parameter names — single
   letters used by physics (`T`, `u`, `v`, `w`, `p`, `E`, `H`, `B`, `J`, …).

**Pitfalls.**
- Hardcoding values downstream instead of referencing parameters — every
  dimension and condition should trace back to a parameter.
- Inconsistent units: defining a length in mm and forgetting the
  conversion where metres are expected. Use COMSOL unit syntax: `10[mm]`.
- Reserved names (`T`, `u`, `p`) as parameters — they shadow solution
  variables and cause silent errors.
- Redundant parameters duplicating built-in constants (`pi` is native).
- Missing descriptions — they appear in the GUI and are essential model
  documentation (Layer A/B lint flags them, see `linting.md`).

**API quick-ref.** `model.param()`: `set(name, expr)`,
`set(name, expr, descr)`, `remove(name)`, `descr(name)`,
`evaluate(name)`; `evaluateUnit(name)` is on `ParamBase` (params only —
[G-VARIABLE-EVALUATEUNIT-MISSING](known-gotchas.md#g-variable-evaluateunit-missing)).
Parameters are global scope. ✓ verified.

---

## Stage 2 — Geometry

**Mindset.** Build from primitives and boolean operations. This is the most
failure-prone stage — topology errors cascade through everything
downstream. Work incrementally: add one feature, run the geometry, verify,
then add the next. Prefer simple primitives over imported CAD. Use
parameters for all dimensions.

**Invariants.**
1. Tag every geometry feature through TagRegistry with a type prefix
   (`blk`, `cyl`, `sph`, `con`, `tor`, `wp`, `ext`, `rev`, `uni`, `dif`,
   `int`, `fil`, `chm`, `arr`, `imp`, …) — `facade-conventions.md`.
2. Call `geom.run()` (or `run(tag)`) after adding features — features are
   not realized until run.
3. The geometry stage MAY use entity indices internally during
   construction (work-plane attachments, partitions, fillet/chamfer edge
   picks). This is the one explicit exemption from the selection
   discipline.
4. Produce every geometric entity that downstream stages will reference
   via named selections; verify entity counts after the final run.
5. Reference all dimensions via parameters — no hardcoded lengths,
   positions, or angles.
6. Never re-import / replace a geometry sequence on an existing model —
   it silently wipes every selection bound to it
   ([G-SELECTION-WIPE-ON-GEOM-REIMPORT](known-gotchas.md#g-selection-wipe-on-geom-reimport)).

**Pitfalls.**
- Boolean union of non-touching bodies creates a multi-domain assembly,
  not a merged domain — use Union only when bodies share faces; set `keep`
  false to discard inputs.
- Thin features (aspect ratio > 100:1) cause mesh failures — consider a
  boundary-layer mesh instead of resolving the layer geometrically (and
  see `meshing.md` on thin laminate sheets).
- Forgetting `geom.run()` — the tree shows the feature but the meshable
  geometry is stale.
- Work planes not properly attached to the 3D geometry — always specify
  origin and orientation relative to the model.
- Import features pointing at missing files or formats COMSOL cannot read;
  CAD import also needs the ext library path
  ([G-LIBPATH-CAD-IMPORT](known-gotchas.md#g-libpath-cad-import)).
- Over-complex geometry when simple approximations suffice (a cylinder for
  a bolt is usually adequate for thermal/structural analysis).
- Fillet/chamfer failing because the selected edges are too short or the
  radius too large relative to adjacent features.

**API quick-ref.** `model.component(comp).geom(geomTag)`: `create(tag, type)`,
`feature(tag).set(prop, value)`, `feature(tag).setIndex(prop, value, i)`,
`run()`, `run(tag)`. Primitives ✓: `Block`, `Cylinder`, `Sphere`, `Cone`,
`Torus`, `Polygon`, `BezierPolygon` (2D: `Rectangle`, `Circle`, `Square`,
`Interval`). Operations ✓: `Union`, `Difference`, `Intersection`, `Compose`,
`Copy`, `Move`, `Mirror`, `Rotate`, `Scale`, `Array`, `Fillet`, `Chamfer`,
`Partition`. Construction ✓: `WorkPlane`, `Extrude`, `Revolve`, `Sweep`.
`Import` ✓. Geometry sequences also expose `getNDomains()` etc. for
census probes (see `meshing.md` §5).

---

## Stage 3 — Selections

**Mindset.** The most critical stage for robustness: every boundary,
domain, edge, and point that ANY downstream stage references gets a named
selection here. Use geometric predicates (Box, Ball, Cylinder, Adjacent)
that survive re-parameterization; explicit index selections break when the
geometry changes — last resort only. Think about what downstream needs:
materials need domains, physics needs boundaries and domains, mesh needs
refinement regions.

**Invariants.**
1. Create all selections through SelectionAlgebra (serializable,
   auditable, survives re-parameterization).
2. Names are descriptive and unambiguous: `top_surface`, `inlet_boundary`,
   `core_domain` — not `sel1`.
3. Primitive selections specify ALL required parameters: Box needs six
   bounds plus a condition (inside / intersects); Ball needs a centre and
   radius.
4. Boolean selections (Union, Intersection, Complement, Difference,
   Adjacent) reference existing selection names as operands.
5. After this stage, integer entity indices are FORBIDDEN in materials,
   physics, mesh, studies, and postprocessing (the selection discipline —
   `facade-conventions.md`).
6. Create a selection for EVERY entity a downstream stage will need;
   missing ones force a regression back to this stage.

**Pitfalls.**
- Box bounds that miss the intended entities by epsilon — use parameter
  expressions with small offsets (`height - 0.001[m]`) rather than exact
  coordinates; COMSOL's geometric tolerance is ~1e-6 relative.
- Adjacent selections picking up unexpected entities — Adjacent finds ALL
  entities of the target dimension touching the source; verify the count.
- Missing selections for boundaries physics or mesh need — every BC, size
  control, and material assignment needs one; audit downstream needs
  before finishing.
- Overlapping selections causing ambiguous material assignment — COMSOL
  silently uses the last assignment.
- Explicit selections that break on re-parameterization — a domain that
  was index 3 becomes 5 after a fillet; use predicates instead. (Measured
  consequence in meshing: [G-SWEPT-SOURCEFACE-READBACK](known-gotchas.md#g-swept-sourceface-readback).)

**API quick-ref (facade).** `SelectionAlgebra`: `createBox(name, bounds)`,
`createBall(name, params)`, `createCylinder(name, params)`,
`createExplicit(name, dim, entities)`, `createUnion / createIntersection
(name, operands…)`, `createComplement(name, operand)`,
`createDifference(name, base, subtract)`, `createAdjacent(name, source,
targetDim)` — full tables in `facade-conventions.md`. Native COMSOL
selections live under `model.component(comp).selection()` (`Box`,
`Ball`, `Cylinder`, `Explicit`, `Union`, `Difference`, `Adjacent`, …). ✓

---

## Stage 4 — Materials

**Mindset.** Assign materials to domains via named selections. Use the
built-in library for standard materials (copper, steel, air, water) —
library materials carry validated temperature-dependent properties.
Custom materials need every property the active physics requires
(thermal conductivity for heat transfer, Young's modulus for solid
mechanics, electrical conductivity for electric currents).

**Invariants.**
1. All domain assignments via named selections — no entity indices.
2. Every domain has exactly one material; unassigned domains fail the
   solve with cryptic errors.
3. Multiphysics needs properties for ALL active physics (Joule heating:
   electrical *and* thermal conductivity; thermoelastic: expansion
   coefficient, Young's modulus, Poisson's ratio, *and* thermal
   conductivity).
4. Track material entries through TagRegistry (prefix `mat`).
5. Verify no domain has multiple materials — COMSOL silently uses the last
   assignment.

**Pitfalls.**
- Missing properties for the active physics — SILENT at build time; the
  solver fails with "undefined property" or produces meaningless results.
- Constant properties where temperature dependence matters — copper's
  thermal conductivity varies ~2× between 300 K and 800 K.
- Air/vacuum on solid domains or solid material on fluid domains — check
  phase against domain type.
- Overlapping assignments via overlapping selections — audit that the
  selection coverage is an exact partition (no overlaps, no gaps).
- Built-in property names must match what the physics interface expects
  exactly; custom property groups may use different naming.

**API quick-ref.** `model.component(comp).material()`: `create(tag)` for
custom, `create(tag, "Common")` ✓ for library materials; assignment
`material(tag).selection().named(selName)`; properties
`material(tag).propertyGroup("def").set(key, value)` ✓. Common keys ✓:
`thermalconductivity`, `heatcapacity`, `density`, `youngsmodulus`,
`poissonsratio`, `electricconductivity`, `relpermittivity`,
`relpermeability`. `material(tag).set("family", …)` ✓ sets the *appearance*
family (`steel`, `copper`, `water`, `custom`, …) — it is not a library
lookup; library materials are created with `create(tag, "Common")` and
their property groups populated explicitly.

---

## Stage 5 — Physics

**Mindset.** Configure interfaces with boundary conditions, initial
conditions, sources, and constraints — where the science becomes COMSOL
constructs. Work from the governing equations outward: domain equations
first (sources, sinks, body loads), then boundary conditions (Dirichlet,
Neumann, Robin), then edge/point constraints. For coupled physics,
configure each interface fully before setting up couplings.

**Invariants.**
1. ALL boundary/domain/edge/point references via named selections.
2. Track physics features through TagRegistry (prefix `phys`).
3. Every active interface has *sufficient* boundary conditions for a
   well-posed problem: too few → singular matrix; too many on one boundary
   → over-constrained.
4. Set initial conditions for transient and eigenfrequency studies —
   default zeros can stall nonlinear convergence.
5. Coupled physics reference compatible variable names: Heat Transfer
   exports `T`; Solid Mechanics `u,v,w` (or `solid.disp`); Laminar Flow
   `u,v,w,p` (or `spf.U`). Check coupling variables.
6. Every feature's selection contains entities of the correct dimension
   (domain = 3D, boundary = 2D, edge = 1D, point = 0D).
7. Custom PDE interfaces (`GeneralFormPDE`, `WeakFormPDE`,
   `CoefficientFormPDE`, `GlobalEquations`) MUST set
   `DependentVariableQuantity` + `SourceTermQuantity` (or `Custom*Unit`)
   explicitly — enforced by Layer A rule A6 and the slot catalog
   (`linting.md`).
8. Reach `physics` through the component
   ([G-COMP-METHOD-NEEDS-COMPONENT](known-gotchas.md#g-comp-method-needs-component)).

**Pitfalls.**
- Wrong BC type: a fixed temperature is Dirichlet, a heat flux is Neumann;
  a Dirichlet BC on an *internal* boundary removes degrees of freedom the
  adjacent domain needs.
- Missing initial conditions for transient problems — Newton may fail on
  the first step from a zero start with strong nonlinearity.
- Conflicting BCs on the same boundary (fixed temperature *and* heat flux).
- Wrong interface for the regime: Laminar Flow where the Reynolds number
  indicates turbulence (> ~4000 for pipe flow); Heat Transfer in Solids on
  a domain that includes fluid.
- Forgotten symmetry BCs on symmetry planes (thermal insulation, roller
  constraint, …).
- Variable-coupling errors: referencing `T` in Solid Mechanics when the
  heat interface uses a different component name.

**API quick-ref.** `model.component(comp).physics()`:
`create(tag, interfaceType, geomTag)`; features
`physics(tag).create(featureTag, featureType)`; selection
`physics(tag).feature(ftag).selection().named(selName)`; properties
`physics(tag).feature(ftag).set(prop, value)`. Interface identifiers
verified in the manuals: `HeatTransfer` ✓, `Electrostatics` ✓,
`ElectromagneticWaves` ✓, `ConductiveMedia` ✓ (electric currents).
⚠ The original preambles listed `SolidMechanics`, `LaminarFlow`,
`TurbulentFlowKEpsilon`, `ElectricCurrents`, `MagneticFields`,
`AcousticsPressure` as identifiers — these did NOT verify; confirm the
exact string via `list_physics_options` / `search_api` before use.
Default tags (`ht`, `solid`, `spf`, `ec`, `mf`, `acpr`) are conventional,
not identifiers.

---

## Stage 6 — Mesh

**Mindset.** Start coarse, solve, then refine: a coarse mesh that converges
in 30 s beats a fine mesh that takes 30 min and might fail. Use named
selections for size controls — refine where gradients are steep (boundary
layers, material interfaces, geometric transitions), leave bulk coarse.
Match element type to geometry: free tetrahedral for complex 3D, swept
for extrudable geometry, boundary layers near walls in fluid problems.

For anything beyond the basics — commit semantics, census-not-throw,
sequence ordering, swept sourcing, quality measures, the one-JVM rule —
**[`meshing.md`](meshing.md) is authoritative**; it is campaign-measured.

**Invariants.**
1. Size controls and distributions reference named selections only.
2. Track mesh features through TagRegistry (prefix `mesh`).
3. Every domain is meshed — verify with a census after building; an
   exception does not mean nothing was meshed
   ([G-MESH-THROW-COMMITS-PARTIAL](known-gotchas.md#g-mesh-throw-commits-partial)).
4. Element type is compatible with the physics (quadratic for stress
   accuracy in solid mechanics; linear for stability in CFD).
5. Start at a "Normal"/"Coarse" predefined size (`hauto` 5 or 7); refine
   only after the model solves.

**Pitfalls.**
- Too fine an initial mesh — wasted time on a model that may still have
  physics/geometry errors.
- Thin features skipped by the mesher — a `Size` feature with smaller
  `hmax` on those selections (measured behaviour of thin laminate sheets:
  `meshing.md` §3, [G-DISTRIBUTION-NUMELEM-IS-REQUEST](known-gotchas.md#g-distribution-numelem-is-request)).
- Boundary-layer mesh on non-fluid boundaries — boundary layers belong at
  no-slip walls in CFD, not inlets/outlets or structural boundaries.
- Minimum element quality below ~0.01 causes solver trouble — check mesh
  statistics *with the measure stated*
  ([G-MESHSTATS-DEFAULT-VOLCIRCUM](known-gotchas.md#g-meshstats-default-volcircum)).
- Swept mesh on geometry that isn't truly extrudable — sweeping needs a
  source and destination face connected by a mapping
  ([G-SWEPT-AUTOSOURCE-DECLINE](known-gotchas.md#g-swept-autosource-decline)).
- Incompatible sizes across material interfaces → discontinuous fields;
  keep sizing consistent on shared boundaries
  ([G-MESH-FIRST-MESHER-OWNS-BOUNDARY](known-gotchas.md#g-mesh-first-mesher-owns-boundary)).

**API quick-ref.** `model.component(comp).mesh()`: `create(tag)`,
`mesh(tag).create(featureTag, type)`. Feature types ✓: `FreeTet`,
`FreeTri`, `FreeQuad`, `Swept`, `Map`, `BndLayer`, `Edge`, `Size`,
`Distribution`, `Copy`, `Refine`, `Convert`, `CornerRefinement`.
(The original preambles named `FreeTriangular`, `MappedMeshing`,
`BoundaryLayerMeshing`, `EdgeMeshing` — those are **not** the API strings.)
Size ✓: `set("hauto", level)` (1 = extremely fine … 9 = extremely coarse),
`hmax`, `hmin`, `hgrad`, `hcurve`, `hnarrow`, `custom`. Boundary layer:
`blnlayers` ✓, `blstretch` ✓, `blhtot` ✓, `blhminfact` ✓, `blhmin` ✓
(the preambles' `bltfirst` did not verify). Sweep distribution:
`Distribution` child with `numelem` ✓ (a *request*, not a command). Run
the whole sequence (`ms.run()`); prefer `edit-mph --mesh <tag>` which
adds heartbeat, census, and per-feature build records.

---

## Stage 7 — Studies

**Mindset.** The study defines WHAT to solve (which physics, which study
type); the solver defines HOW (direct vs iterative, tolerances, time
steps). Match the study type to the physics: Stationary for steady state,
Time Dependent for transients, Frequency Domain for harmonic excitation,
Eigenfrequency for modal analysis. Use default solver settings first —
manual tuning only when convergence fails.

**Invariants.**
1. Study type matches the physics (transient physics → time-dependent
   study; mismatches give wrong or no results).
2. All relevant interfaces are included in the study step — omitting one
   silently skips it.
3. Track studies (`stdy`) and solvers (`sol`) through TagRegistry.
4. Time-dependent studies specify the time range — `range(0, dt, t_end)`.
5. Coupled multiphysics: the step includes every interface solved
   simultaneously; segregated approaches need explicit configuration.
6. Domain-specific study settings use named selections.

**Pitfalls.**
- Wrong study type: Time Dependent on a stationary problem wastes time; a
  Stationary study on a transient problem shows only the (possible) final
  state.
- Missing physics in the step — silent, incomplete results.
- Time steps too large for fast transients (shocks, rapid switching) —
  set a maximum time step.
- Tolerances too tight (slow / failing convergence) or too loose
  (inaccurate); the default relative tolerance 1e-3 suits most engineering
  problems.
- Switching to a segregated solver without configuring the coupling
  variables (the default is fully coupled).
- Eigenfrequency: forgetting the number of eigenvalues or misplacing the
  search region.
- `consistent`/initialisation flags need canonical lowercase tokens
  ([G-CONSISTENT-OFF-TOKEN](known-gotchas.md#g-consistent-off-token)).

**API quick-ref.** `model.study()`: `create(tag)`,
`study(tag).create(stepTag, type)` with step types ✓ `Stationary`,
`Transient`, `Frequency`, `Eigenfrequency`, `Eigenvalue`, `Parametric`
(the preambles' `FrequencyDomain` / `FrequencyDomainModal` did not verify —
the harmonic step type is `Frequency`). Step settings ✓: `set("tlist",
"range(0,dt,tend)")`, `set("neigsactive", true)`, `set("neigs", n)`.
Solver ✓: `model.sol().create(tag)`, `sol(tag).create(stepTag,
"StudyStep")`, `Stationary`/`Variables` solver features;
`set("reltol", v)` ✓; on the `Time` solver feature `atolglobal` ✓ is the
single global absolute tolerance and `atol` ✓ is a space-separated
per-field string (`"u 1e-3 v 1e-6"`, paired with `atolmethod`). There is
no per-step solve callback — use the heartbeat
([G-NO-PER-STEP-SOLVER-CALLBACK](known-gotchas.md#g-no-per-step-solver-callback)).

---

## Stage 8 — Postprocessing

**Mindset.** Extract exactly what the intent asked for: derived values
(min/max/average/integral over selections), plots, CSV exports. Always
sanity-check results against known benchmarks or order-of-magnitude
estimates — temperature ranges, stress magnitudes, velocities. If results
look wrong, flag them; never silently export nonsense.

**Invariants.**
1. Evaluation domains/boundaries via named selections only.
2. Track postprocessing entries through TagRegistry (prefix `pp`).
3. Postprocess only after a successful solve — check the solution exists
   and converged.
4. Export paths stay inside the build artifact directory — never system
   directories or the user's home.
5. Include units in all exported data and derived values; label
   dimensionless results explicitly.
6. At least one sanity check per physics interface (physical temperature
   range? stress below ultimate strength? velocity below the speed of
   sound for incompressible flow?).

**Pitfalls.**
- Evaluating on the wrong selection — a boundary value where a domain
  average was wanted, or all boundaries where one face was intended.
- Unit conversion in derived values — COMSOL works in SI internally; apply
  conversions explicitly for mm, MPa, degC outputs.
- Exporting before the solve completes — partial solutions from a failed
  solve are garbage.
- Plot resolution too low for sharp features (boundary layers, stress
  concentrations) — raise the evaluation-point count.
- Missing sanity checks — 1e6 K in a heat sink means the model is wrong; a
  negative pressure in incompressible flow is meaningful but often signals
  poor mesh or BCs.
- Forgetting to export the mesh alongside solution data when results go to
  another tool.

**API quick-ref.** Plot groups ✓: `model.result().create(tag, type)` with
`PlotGroup1D` / `PlotGroup2D` / `PlotGroup3D`; plot features ✓
`result(tag).create(ftag, type)` with `Surface`, `Volume`, `Line`,
`ArrowSurface`/`ArrowVolume`/`ArrowLine`/`ArrowPoint` (there is no bare
`Arrow` type), `Contour`, `Streamline`, `Isosurface`, `Slice`. Numerical evaluation ✓:
`model.result().numerical().create(tag, type)` with `EvalPoint`,
`EvalGlobal`, `AvLine`, `AvSurface`, `AvVolume`, `IntLine`, `IntSurface`,
`IntVolume`, `MaxSurface`, `MaxVolume`, `MinSurface` (the preambles'
`PointEval`, `LineAverage`, `SurfaceAverage`, `VolumeAverage`,
`VolumeIntegration`, `SurfaceIntegration` are **not** the API strings).
Export ✓: `model.result().export().create(tag, type)` with `Data`,
`Plot`, `Table`, `Image`, `Animation`, `Mesh` (the two-argument forms
`create(tag, plotGroupTag, "Image")` / `create(tag, datasetTag, "Data")`
bind the source at creation). Settings: `set("expr", …)`, `set("unit", …)`,
`selection().named(selName)`. Probe values are read via reflection
([G-PROBE-GETREAL-MISSING](known-gotchas.md#g-probe-getreal-missing)).

---

## Stage 9 — Functions

**Mindset.** Functions express non-trivial spatial or temporal dependence:
interpolation tables for measured material data, analytic expressions for
varying loads, step functions for switching boundary conditions, piecewise
functions for multi-stage processes. Define every function before any
physics feature references it.

**Invariants.**
1. Functions are defined before any reference — COMSOL resolves names at
   build time; forward references fail.
2. Track functions through TagRegistry (prefix `fn`).
3. Interpolation functions specify units for both the argument and the
   result columns — omitting them causes silent dimension mismatches.
4. Function names are global — unique across the model.
5. Descriptive names matching the physical role (`k_vs_T`).

**Pitfalls.**
- Wrong interpolation method — linear on data with sharp transitions; use
  piecewise-cubic for smooth data, nearest-neighbour for step-like data.
- Extrapolation behaviour unset — the default can diverge the solver when
  the solution leaves the table range; set `extrap` to `const` or `none`
  explicitly.
- Analytic functions with undefined regions (`sqrt(x)` for negative `x`) —
  add `abs()`/`max()` guards.
- Argument-unit mismatch with the calling context — a function defined on
  `[K]` fails when called with a `[degC]` variable.

**API quick-ref.** `model.func()`: `create(tag, type)` with types ✓
`Analytic`, `Interpolation`, `Piecewise`, `Step`, `Ramp`, `Rectangle`,
`Triangle`, `Wave`, `GaussianPulse`, `NormalDistribution`, `Random`,
`External`, `Elevation` (the preambles' `Gaussian` / `Waveform` are **not**
the API strings — they are `GaussianPulse` / `Wave`). Keys ✓:
`funcname`, `expr`, `setIndex("table", value, row, col)`, `interp`,
`extrap`, `argunit`, `fununit`.
