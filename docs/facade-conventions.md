# Facade conventions — TagRegistry and SelectionAlgebra

The two pure-Java facade classes in `comsol_support/java/` that give model
authoring deterministic, auditable names: **`TagRegistry`** (model-tree
tags) and **`SelectionAlgebra`** (named geometric selections). Both are
plain Java with no `com.comsol.model.*` imports — they track *definitions*
as data; the code that consumes them emits the corresponding COMSOL API
calls. Both serialize to JSON so a build's naming state can be persisted
and restored.

Provenance: harvested 2026-08 from the retired orchestrator's
`_protocol.xml` (tag / selection discipline) and validators, when L5 was
deprecated. The conventions apply to any builder or mutator that uses the
facade; nothing enforces them automatically any more, so they are stated
here as the working contract.

---

## 1. Tag discipline

**Rule.** Every COMSOL model-tree node the code creates and references
goes through `TagRegistry`. Raw tag strings (`"blk1"`, `"sel3"`) are not
written by hand in generated Java.

**Why.** Raw tag strings are the #1 source of silent failures in automated
COMSOL builds: a misspelled tag *compiles* but produces the wrong physics.
TagRegistry assigns, tracks, and validates tags by construction, and its
condensed view lets a reader (or an agent mid-build) see what exists.

### Canonical tag prefixes

| Prefix | Node type | Prefix | Node type |
|---|---|---|---|
| `blk` | Block | `cyl` | Cylinder |
| `sph` | Sphere | `con` | Cone |
| `tor` | Torus | `wp` | WorkPlane |
| `ext` | Extrude | `rev` | Revolve |
| `arr` | Array | `imp` | Import |
| `prt` | Partition | `mir` | Mirror |
| `comp` | Component | `geom` | Geometry |
| `par` | Parameter | `fn` | Function |
| `var` | Variable | `mat` | Material |
| `phys` | Physics | `mesh` | Mesh |
| `stdy` | Study | `sol` | Solver |
| `pp` | Postprocessing | | |

Selection prefixes (owned by SelectionAlgebra, §2): `boxsel`, `ballsel`,
`cylsel`, `expsel` (primitives); `uni`, `int`, `cmp`, `dif`, `adj`
(boolean operations). Geometry booleans in the original convention also
used `uni`/`dif`/`int` plus `fil` (Fillet) and `chm` (Chamfer).

### TagRegistry API

| Method | Purpose |
|---|---|
| `String create(String name, String prefix, String type, String parent, String stage)` | Register a node under a human-readable `name`; returns the generated tag (`prefix` + auto-increment, e.g. `blk1`). `parent` is the owning node's name; `stage` labels the build stage that created it. Throws on an empty name or a duplicate. |
| `String get(String name)` | Tag for a registered name. |
| `TagEntry getEntry(String name)` / `TagEntry getByTag(String tag)` | Full record (`tag`, `name`, `type`, `parent`, `stage`). |
| `boolean exists(String name)` / `boolean tagExists(String tag)` | Membership checks. |
| `List<TagEntry> getByStage(String stage)` / `getByType(String type)` / `getChildren(String parentName)` | Queries used for auditing and condensed views. |
| `int size()` | Number of registered nodes. |
| `String serialize()` / `static TagRegistry deserialize(String json)` | JSON round-trip for persistence. |
| `String toCondensedView()` | Compact multi-line listing (`[stage] tag=name (Type, parent=…)`) for human or agent inspection. |

Usage sketch inside a builder/mutator:

```java
TagRegistry tags = new TagRegistry();
String blk = tags.create("substrate", "blk", "Block", "geom1", "geometry");
model.component("comp1").geom("geom1").create(blk, "Block");
model.component("comp1").geom("geom1").feature(blk).set("size", new String[]{"L","W","H"});
// later stages reference tags.get("substrate"), never "blk1" literally
```

---

## 2. Selection discipline

**Rule.** All boundary, domain, edge, and point references in the
**materials, physics, mesh, studies, and postprocessing** stages use named
selections created in the selections stage. Integer entity indices are
forbidden there.

**Exemption.** The **geometry** stage (and the intent, parameters,
functions, and selections stages) may use entity indices internally —
work-plane attachments, partitions, and fillet/chamfer edge picks need
them during construction — but the geometry stage must *produce* named
selections for every entity downstream stages will reference.

**Why.** Named selections built from geometric predicates (box, ball,
cylinder, adjacent-to, complement, union, intersection, difference)
survive geometry re-parameterization; integer indices renumber whenever
the geometry changes (a domain that was index 3 becomes 5 after a fillet)
and after mesh operations. The measured cost of violating this in a
meshing campaign is recorded in
[G-SWEPT-SOURCEFACE-READBACK](known-gotchas.md#g-swept-sourceface-readback)
and `meshing.md` §2.

### Selection kinds

| Kind | Facade method | Parameters | Notes |
|---|---|---|---|
| Box | `createBox(name, bounds)` | map with `xmin xmax ymin ymax zmin zmax` and `condition` (`inside` / `intersects`) | use parameter expressions with small offsets (`height - 0.001[m]`), not exact coordinates; COMSOL's geometric tolerance is ~1e-6 relative |
| Ball | `createBall(name, params)` | `cx cy cz radius` + `condition` | |
| Cylinder | `createCylinder(name, params)` | `cx cy cz radius height axis` + `condition` | |
| Explicit | `createExplicit(name, dimension, entities)` | entity dimension + `int[]` ids | **escape hatch, geometry stage only** |
| Union / Intersection | `createUnion(name, operands…)` / `createIntersection(name, operands…)` | existing selection names | operands must already exist (validated) |
| Complement | `createComplement(name, operand)` | one selection name | |
| Difference | `createDifference(name, base, subtract)` | base minus subtract | |
| Adjacent | `createAdjacent(name, source, targetDimension)` | source selection + target entity dimension | finds ALL entities of the target dimension touching the source — verify the count |

Other API: `String getTag(String name)`, `SelectionDef get(String name)`,
`boolean exists(String name)`, `boolean isPrimitive(String name)` /
`isComposite(String name)`, `int size()`,
`List<String> resolveOperands(String name)` (recursively resolves a
composite selection to its primitive tags), `serialize()` /
`deserialize(json)`, `toCondensedView()`.

Naming: descriptive and unambiguous — `top_surface`, `inlet_boundary`,
`core_domain`; never `sel1` / `my_selection`.

### Consuming a selection in COMSOL code

```java
SelectionAlgebra sels = new SelectionAlgebra();
Map<String,String> b = new LinkedHashMap<>();
b.put("xmin","0"); b.put("xmax","L"); b.put("ymin","0"); b.put("ymax","W");
b.put("zmin","H-0.001[m]"); b.put("zmax","H+0.001[m]"); b.put("condition","inside");
String top = sels.createBox("top_surface", b);           // returns e.g. "boxsel1"
// emit the native selection, then reference it by NAME downstream:
model.component("comp1").physics("ht").feature("hf1").selection().named(top);
```

Audit before leaving the selections stage: does every downstream need
(each BC, each mesh size control, each material assignment, each
evaluation) have a selection, and is material coverage an exact
partition (no overlaps — COMSOL silently keeps the last assignment — and
no gaps)?

---

## 3. Where these conventions are used

- `docs/modeling-practice.md` — every stage's invariants reference the tag
  and selection disciplines.
- `docs/meshing.md` §2 — "never write raw integer entity indices into
  selections"; mesh-feature selections derive from geometric rules.
- Campaign probes (`comsol_support/java/probes/`) enumerate feature and child
  selections so violations can be *measured* on an existing model.
