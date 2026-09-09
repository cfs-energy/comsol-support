/**
 * SlotHarvester — Phase 1 of the slot expected-unit catalog pipeline.
 *
 * Loads .mph files in a batch (one JVM init), walks every physics feature,
 * and emits one JSONL record per expression-valued slot to stdout. A
 * separate Python aggregator consumes the dump.
 *
 * Each record carries the five-axis key used by the catalog:
 *   (physics_type, sdim, feature_type, feature_scope, slot_property)
 * plus the raw expression, source-of-type annotation, filter source,
 * source .mph path, and COMSOL version.
 *
 * Also emits variable_declared_unit records harvested from component
 * variable tags (CustomSourceTermUnit / CustomDependentVariableUnit) so
 * Source B can feed Layer C's symbol resolver.
 *
 * Usage:
 *   java SlotHarvester --list <paths.txt> --out <dump.jsonl>
 *                      [--progress]
 *
 * stdin protocol: one JSON line per emitted record. Progress lines
 * (if --progress) carry {"progress":true,"index":N,"total":M,"mph":"..."}.
 * Terminator: {"done":true,"models":N,"slots":M,"var_units":K,"errors":E}.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.io.PrintStream;
import java.io.Writer;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

public class SlotHarvester {

    // Expression heuristic: property value is treated as expression-valued
    // when it contains a unit literal, an arithmetic operator, or a
    // function-call-like pattern. Pure booleans ("on"/"off"), tags, and
    // integer-only values are excluded. Empty strings are excluded.
    private static final Pattern EXPRESSION_HEURISTIC = Pattern.compile(
        "\\[[^\\]]+\\]"        // unit literal, e.g. [m], [W/m^2]
        + "|[+\\-*/^]"         // arithmetic operator
        + "|\\b[A-Za-z_][A-Za-z_0-9]*\\s*\\("  // function call
        + "|\\b[A-Za-z_][A-Za-z_0-9]*\\b\\s*[+\\-*/^]" // identifier arithmetic
    );

    // Strings that should never be treated as expressions even when they
    // contain characters that match the heuristic above.
    private static final Pattern BOOLEAN_VALUES = Pattern.compile(
        "^\\s*(on|off|true|false|yes|no|none|auto|default)\\s*$",
        Pattern.CASE_INSENSITIVE);

    // Property-name heuristics for Source B: keys that look like unit
    // declarations. We harvest these per component variable.
    private static final Pattern UNIT_DECL_KEY = Pattern.compile(
        "Unit$|^unit$|^physicalUnit$",
        Pattern.CASE_INSENSITIVE);

    public static void main(String[] argv) {
        String listPath = null;
        String outPath = null;
        String versionArg = null;
        boolean progress = false;

        for (int i = 0; i < argv.length; i++) {
            String a = argv[i];
            if ("--list".equals(a) && i + 1 < argv.length) {
                listPath = argv[++i];
            } else if ("--out".equals(a) && i + 1 < argv.length) {
                outPath = argv[++i];
            } else if ("--version".equals(a) && i + 1 < argv.length) {
                versionArg = argv[++i];
            } else if ("--progress".equals(a)) {
                progress = true;
            } else {
                emitStderr("Unknown argument: " + a);
                System.exit(1);
            }
        }

        if (listPath == null || outPath == null) {
            emitStderr("Required: --list <paths.txt> --out <dump.jsonl>");
            System.exit(1);
        }

        List<String> mphPaths;
        try {
            mphPaths = Files.readAllLines(Paths.get(listPath));
            mphPaths.removeIf(s -> s == null || s.trim().isEmpty());
        } catch (IOException e) {
            emitStderr("Cannot read list file: " + e.getMessage());
            System.exit(1);
            return;
        }

        // Prefer the --version passed by the Python driver, slot_harvest.py (which
        // reads the actual install version from COMSOL config files);
        // fall back to ModelUtil reflection, then to System property.
        final String versionArgFinal = versionArg;
        String comsolVersion = safeString(() -> {
            if (versionArgFinal != null && !versionArgFinal.isEmpty()) {
                return versionArgFinal;
            }
            // Try reflection on ModelUtil for a getCOMSOLVersion or
            // similarly-named static. Names vary by COMSOL version and
            // obfuscation; all failures fall through silently.
            for (String mname : new String[]{
                "getCOMSOLVersion", "getComsolVersion", "version",
                "getVersion"
            }) {
                try {
                    Method m = ModelUtil.class.getMethod(mname);
                    Object o = m.invoke(null);
                    if (o != null) return o.toString();
                } catch (Throwable ignore) {
                }
            }
            return System.getProperty("cs.version", "unknown");
        });

        int models = 0;
        int slots = 0;
        int varUnits = 0;
        int errors = 0;

        try {
            ModelUtil.initStandalone(false);
        } catch (Exception e) {
            emitStderr("Failed to initialize COMSOL: " + e.getMessage());
            System.exit(1);
            return;
        }

        Path out = Paths.get(outPath);
        try {
            if (out.getParent() != null) Files.createDirectories(out.getParent());
        } catch (IOException e) {
            emitStderr("Cannot create output directory: " + e.getMessage());
            System.exit(1);
            return;
        }

        try (BufferedWriter w = Files.newBufferedWriter(
                out, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {

            for (int idx = 0; idx < mphPaths.size(); idx++) {
                String mphPath = mphPaths.get(idx).trim();
                if (mphPath.isEmpty()) continue;
                String tag = "slotharvest_" + idx;

                if (progress) {
                    emitProgress(idx + 1, mphPaths.size(), mphPath);
                }

                try {
                    Model model = ModelUtil.load(tag, mphPath);
                    HarvestResult r = harvestModel(
                        model, mphPath, comsolVersion, w);
                    slots += r.slots;
                    varUnits += r.varUnits;
                    models++;
                    try {
                        ModelUtil.remove(tag);
                    } catch (Exception ignore) {}
                } catch (Throwable e) {
                    errors++;
                    emitStderrLoad(mphPath, e);
                    // Try to remove the tag even if load partially succeeded
                    try {
                        ModelUtil.remove(tag);
                    } catch (Exception ignore) {}
                }
            }
        } catch (IOException e) {
            emitStderr("Output write failed: " + e.getMessage());
            System.exit(1);
            return;
        }

        emit(String.format(
            "{\"done\":true,\"models\":%d,\"slots\":%d,\"var_units\":%d,"
          + "\"errors\":%d}",
            models, slots, varUnits, errors));

        System.exit(0);
    }

    // ---- Per-model harvest ----

    public static class HarvestResult {
        public int slots;
        public int varUnits;
    }

    /** Walk one loaded model and emit slot + var_unit JSONL records to
     *  the given writer. Public so that ModelChecker can reuse this
     *  walk within its own JVM session, avoiding a second COMSOL load.
     */
    public static HarvestResult harvestLoadedModel(
            Model model, String mphPath, String comsolVersion,
            BufferedWriter w) throws IOException {
        return harvestModel(model, mphPath, comsolVersion, w);
    }

    private static HarvestResult harvestModel(
            Model model, String mphPath, String comsolVersion,
            BufferedWriter w) throws IOException {
        HarvestResult r = new HarvestResult();

        // sdim is a per-geometry property. We build a map of geom tag -> sdim
        // string so each physics interface can look up the geometry it uses.
        String[] geomTags = safeStringArray(() -> model.geom().tags());
        java.util.Map<String, String> geomSdim = new java.util.HashMap<>();
        for (String g : geomTags) {
            String sdim = resolveSdim(model, g);
            geomSdim.put(g, sdim);
        }
        String defaultSdim = geomTags.length > 0
            ? geomSdim.getOrDefault(geomTags[0], "unknown")
            : "unknown";

        String[] compTags = safeStringArray(() -> model.component().tags());
        if (compTags == null || compTags.length == 0) {
            // Older API: walk via modelNode() as a fallback.
            compTags = safeStringArray(() -> model.modelNode().tags());
        }
        if (compTags == null) compTags = new String[0];

        // Track which physics tags have been walked within this model,
        // so the top-level sweep at the end only picks up physics that
        // weren't bound to any component (e.g. multiphysics couplings).
        Set<String> seenPhysicsTags = new HashSet<>();

        for (String ctag : compTags) {
            // Harvest Source B records from component variables first.
            r.varUnits += harvestVariableUnits(
                model, ctag, defaultSdim, mphPath, comsolVersion, w);

            // Walk physics interfaces SCOPED TO THIS COMPONENT.
            // Using model.physics() (top-level) here would emit the
            // same slots once per component on multi-component models.
            // The component-scoped accessor mirrors NodeTreeProbe.java
            // (see lines ~179-202): reflective lookup because the
            // Component type is not in our import set.
            Object comp;
            try {
                comp = model.component(ctag);
            } catch (Throwable t) {
                continue;
            }
            if (comp == null) continue;

            String[] physicsTags;
            try {
                Object plist = comp.getClass().getMethod("physics")
                    .invoke(comp);
                physicsTags = (String[]) plist.getClass().getMethod("tags")
                    .invoke(plist);
            } catch (Throwable t) {
                // Some component kinds may not have physics (e.g.
                // ExtraDim) — skip silently.
                continue;
            }
            if (physicsTags == null) continue;

            for (String ptag : physicsTags) {
                Object physics;
                try {
                    physics = comp.getClass()
                        .getMethod("physics", String.class)
                        .invoke(comp, ptag);
                } catch (Throwable t) {
                    continue;
                }
                if (physics == null) continue;
                seenPhysicsTags.add(ptag);

                String physicsType = safeStringOrNull(() -> {
                    Method m = physics.getClass().getMethod("getType");
                    Object o = m.invoke(physics);
                    return o == null ? null : o.toString();
                });
                String typeSource = "getType";
                if (physicsType == null || physicsType.isEmpty()) {
                    physicsType = bestInterfaceName(physics);
                    typeSource = "interface_fallback";
                }

                String physicsGeom = safeStringOrNull(() -> {
                    Method m = physics.getClass().getMethod("geom");
                    Object o = m.invoke(physics);
                    return o == null ? null : o.toString();
                });
                String sdim = (physicsGeom != null && geomSdim.containsKey(physicsGeom))
                    ? geomSdim.get(physicsGeom)
                    : defaultSdim;

                // Enumerate features.
                String[] ftags;
                try {
                    Object flist = physics.getClass().getMethod("feature")
                        .invoke(physics);
                    ftags = (String[]) flist.getClass().getMethod("tags")
                        .invoke(flist);
                } catch (Throwable t) {
                    continue;
                }
                if (ftags == null) continue;

                for (String ftag : ftags) {
                    Object feature;
                    try {
                        feature = physics.getClass()
                            .getMethod("feature", String.class)
                            .invoke(physics, ftag);
                    } catch (Throwable t) {
                        continue;
                    }
                    if (feature == null) continue;

                    String featureType = safeStringOrNull(() -> {
                        Method m = feature.getClass().getMethod("getType");
                        Object o = m.invoke(feature);
                        return o == null ? null : o.toString();
                    });
                    String ftTypeSource = "getType";
                    if (featureType == null || featureType.isEmpty()) {
                        featureType = feature.getClass().getSimpleName();
                        ftTypeSource = "interface_fallback";
                    }

                    String scope = resolveScope(feature, featureType);

                    // Enumerate properties. PhysicsFeature extends
                    // ExpressionBase on most COMSOL feature kinds, so
                    // varnames() + get(name) is the primary path. Fall
                    // back to getAllString-style probing if unavailable.
                    String[] propNames;
                    try {
                        propNames = (String[]) feature.getClass()
                            .getMethod("properties").invoke(feature);
                    } catch (Throwable t) {
                        propNames = null;
                    }
                    // Some feature classes return String[] directly from
                    // properties(); others wrap in a collection with
                    // varnames(). Probe the wrapper too.
                    if (propNames == null) {
                        try {
                            Object wrap = feature.getClass()
                                .getMethod("properties").invoke(feature);
                            propNames = (String[]) wrap.getClass()
                                .getMethod("varnames").invoke(wrap);
                        } catch (Throwable t) {
                            propNames = null;
                        }
                    }
                    // Last resort: the feature itself is an ExpressionBase.
                    if (propNames == null) {
                        try {
                            propNames = (String[]) feature.getClass()
                                .getMethod("varnames").invoke(feature);
                        } catch (Throwable t) {
                            propNames = new String[0];
                        }
                    }
                    if (propNames == null) propNames = new String[0];

                    for (String pname : propNames) {
                        String expr = readStringProperty(feature, pname);
                        if (expr == null) continue;
                        ExprFilter filter = classifyExpression(expr);
                        if (filter == null) continue;

                        writeSlotRecord(
                            w, mphPath, comsolVersion,
                            ctag,
                            ptag, physicsType, typeSource,
                            sdim,
                            ftag, featureType, ftTypeSource,
                            scope,
                            pname, expr, filter);
                        r.slots++;
                    }
                }
            }
        }

        // Safety sweep — pick up any model-level physics not bound to
        // a specific component (multiphysics couplings, shared physics,
        // or anything the component-scoped walk missed via reflection).
        // We attribute these to the first compTag as an approximation;
        // component_tag="(shared)" would be more honest but callers
        // parse component_tag as a regular tag string. Since this is
        // a fallback, we also annotate type_source="top_level_sweep"
        // on the physics side.
        r.slots += sweepTopLevelPhysics(
            model, compTags, seenPhysicsTags, geomSdim, defaultSdim,
            mphPath, comsolVersion, w);

        return r;
    }

    /** Walk top-level model.physics() for any physics NOT already seen
     *  via the per-component walk. Emits slot records with component_tag
     *  set to the first compTag (approximation for shared physics). */
    private static int sweepTopLevelPhysics(
            Model model, String[] compTags, Set<String> seenPhysicsTags,
            java.util.Map<String, String> geomSdim, String defaultSdim,
            String mphPath, String comsolVersion, BufferedWriter w)
            throws IOException {
        int emitted = 0;
        String attributeCtag = compTags.length > 0 ? compTags[0] : "";
        String[] physicsTags;
        try {
            physicsTags = model.physics().tags();
        } catch (Throwable t) {
            return 0;
        }
        if (physicsTags == null) return 0;

        for (String ptag : physicsTags) {
            if (seenPhysicsTags.contains(ptag)) continue;

            Object physics;
            try {
                physics = model.physics(ptag);
            } catch (Throwable t) {
                continue;
            }
            if (physics == null) continue;

            String physicsType = safeStringOrNull(() -> {
                Method m = physics.getClass().getMethod("getType");
                Object o = m.invoke(physics);
                return o == null ? null : o.toString();
            });
            String typeSource = "getType";
            if (physicsType == null || physicsType.isEmpty()) {
                physicsType = bestInterfaceName(physics);
                typeSource = "interface_fallback";
            }

            String physicsGeom = safeStringOrNull(() -> {
                Method m = physics.getClass().getMethod("geom");
                Object o = m.invoke(physics);
                return o == null ? null : o.toString();
            });
            String sdim = (physicsGeom != null && geomSdim.containsKey(physicsGeom))
                ? geomSdim.get(physicsGeom)
                : defaultSdim;

            String[] ftags;
            try {
                Object flist = physics.getClass().getMethod("feature")
                    .invoke(physics);
                ftags = (String[]) flist.getClass().getMethod("tags")
                    .invoke(flist);
            } catch (Throwable t) {
                continue;
            }
            if (ftags == null) continue;

            for (String ftag : ftags) {
                Object feature;
                try {
                    feature = physics.getClass()
                        .getMethod("feature", String.class)
                        .invoke(physics, ftag);
                } catch (Throwable t) {
                    continue;
                }
                if (feature == null) continue;

                String featureType = safeStringOrNull(() -> {
                    Method m = feature.getClass().getMethod("getType");
                    Object o = m.invoke(feature);
                    return o == null ? null : o.toString();
                });
                String ftTypeSource = "getType";
                if (featureType == null || featureType.isEmpty()) {
                    featureType = feature.getClass().getSimpleName();
                    ftTypeSource = "interface_fallback";
                }
                String scope = resolveScope(feature, featureType);

                String[] propNames;
                try {
                    propNames = (String[]) feature.getClass()
                        .getMethod("properties").invoke(feature);
                } catch (Throwable t) {
                    propNames = null;
                }
                if (propNames == null) {
                    try {
                        Object wrap = feature.getClass()
                            .getMethod("properties").invoke(feature);
                        propNames = (String[]) wrap.getClass()
                            .getMethod("varnames").invoke(wrap);
                    } catch (Throwable t) {
                        propNames = null;
                    }
                }
                if (propNames == null) {
                    try {
                        propNames = (String[]) feature.getClass()
                            .getMethod("varnames").invoke(feature);
                    } catch (Throwable t) {
                        propNames = new String[0];
                    }
                }
                if (propNames == null) propNames = new String[0];

                for (String pname : propNames) {
                    String expr = readStringProperty(feature, pname);
                    if (expr == null) continue;
                    ExprFilter filter = classifyExpression(expr);
                    if (filter == null) continue;
                    writeSlotRecord(
                        w, mphPath, comsolVersion,
                        attributeCtag,
                        ptag, physicsType, typeSource,
                        sdim,
                        ftag, featureType, ftTypeSource,
                        scope,
                        pname, expr, filter);
                    emitted++;
                }
            }
            seenPhysicsTags.add(ptag);
        }
        return emitted;
    }

    // ---- Scope resolution ----

    // Name-hint patterns for scope inference. Order matters: the first
    // matching pattern wins. `boundary` checks run before `domain`
    // because "BoundaryProbe" would match both under loose rules.
    private static final String[] BOUNDARY_HINTS = {
        "boundary", "bnd", "flux", "dirichlet", "neumann", "periodic",
        "continuity", "impedance", "scattering", "symmetry", "wall",
        "inlet", "outlet", "antiperiodic", "floquet", "groundplane",
        "electriccurrentsource", "magneticshielding", "impedancebc",
        "openboundary", "absorbingboundary"
    };
    private static final String[] EDGE_HINTS = {
        "edge", "linecurrent", "linecharge", "edgeload", "edgecurrent"
    };
    private static final String[] POINT_HINTS = {
        "point", "pnt", "pointcurrent", "pointcharge", "pointload",
        "pointheat"
    };

    private static boolean matchesAny(String s, String[] needles) {
        if (s == null || s.isEmpty()) return false;
        for (String n : needles) if (s.contains(n)) return true;
        return false;
    }

    private static String resolveScope(Object feature, String featureType) {
        // Primary path — probe more method names for entity dim across
        // COMSOL API versions. Observed empirically: COMSOL's Selection
        // API does not expose any of these via reflection in recent
        // releases, so this path rarely hits, but we keep it for future
        // API evolution.
        try {
            Object sel = feature.getClass().getMethod("selection")
                .invoke(feature);
            if (sel != null) {
                for (String mname : new String[]{
                    "entityDim", "getEntityDim",
                    "inputDim", "getInputDim",
                    "dim", "getDim",
                    "dimension", "getDimension",
                    "dimensionType", "getDimensionType",
                }) {
                    try {
                        Object o = sel.getClass().getMethod(mname).invoke(sel);
                        if (o instanceof Integer) {
                            return scopeFromDim((Integer) o);
                        }
                        if (o instanceof Number) {
                            return scopeFromDim(((Number) o).intValue());
                        }
                    } catch (Throwable ignore) {
                    }
                }
            }
        } catch (Throwable ignore) {
        }

        // Secondary — inspect any boolean accessor that declares scope
        // directly. Covers features that advertise isBoundary / isPoint
        // without an explicit entity dim.
        for (String mname : new String[]{
            "isBoundary", "isBnd", "isEdge", "isPoint",
        }) {
            try {
                Object o = feature.getClass().getMethod(mname).invoke(feature);
                if (Boolean.TRUE.equals(o)) {
                    if (mname.contains("Boundary") || mname.contains("Bnd")) {
                        return "boundary";
                    }
                    if (mname.contains("Edge")) return "edge";
                    if (mname.contains("Point")) return "point";
                }
            } catch (Throwable ignore) {
            }
        }

        // Name-hint fallback — feature type (getType) is the most
        // reliable string; fall through to class simple name.
        String hintType = (featureType == null ? "" : featureType).toLowerCase();
        String hintCls = feature.getClass().getSimpleName().toLowerCase();

        // Boundary hints first — BoundaryProbe would match both boundary
        // and some domain keywords otherwise.
        if (matchesAny(hintType, BOUNDARY_HINTS)
                || matchesAny(hintCls, BOUNDARY_HINTS)) {
            return "boundary";
        }
        if (matchesAny(hintType, EDGE_HINTS)
                || matchesAny(hintCls, EDGE_HINTS)) {
            return "edge";
        }
        if (matchesAny(hintType, POINT_HINTS)
                || matchesAny(hintCls, POINT_HINTS)) {
            return "point";
        }
        return "domain";
    }

    private static String scopeFromDim(int dim) {
        switch (dim) {
            case 3: return "domain";
            case 2: return "boundary";
            case 1: return "edge";
            case 0: return "point";
            default: return "unknown_dim_" + dim;
        }
    }

    // ---- Sdim ----

    private static String resolveSdim(Model model, String geomTag) {
        int sdim = -1;
        try {
            sdim = model.geom(geomTag).getSDim();
        } catch (Throwable ignore) {
        }
        // Axisymmetric: check the geom's axisymmetric attribute if exposed.
        boolean axi = false;
        try {
            Object geom = model.geom(geomTag);
            Method m = geom.getClass().getMethod("axisymmetric");
            Object o = m.invoke(geom);
            if (o instanceof Boolean) axi = (Boolean) o;
        } catch (Throwable ignore) {
        }
        if (axi && sdim > 0) return sdim + "Daxi";
        if (sdim >= 0) return Integer.toString(sdim);
        return "unknown";
    }

    // ---- Variable unit declarations (Source B) ----

    // Method names to probe on an Expr (variable collection) or on
    // a single-variable view for a per-variable declared unit. COMSOL
    // exposes declared units via several API paths across versions.
    // Empty result from all paths is normal — not all models declare
    // per-variable units.
    private static final String[] VAR_UNIT_METHODS_1ARG = {
        // Per-variable accessors (single String argument):
        "physicalUnit", "getPhysicalUnit",
        "unit", "getUnit",
        "getExpressionUnit", "expressionUnit",
    };
    private static final String[] VAR_UNIT_METHODS_2ARG_KEYS = {
        // `getString(varname, "unit")`-style accessors.
        "physicalUnit", "unit", "customSourceTermUnit",
        "customDependentVariableUnit",
    };

    private static int harvestVariableUnits(
            Model model, String ctag, String sdim, String mphPath,
            String comsolVersion, BufferedWriter w) throws IOException {
        int count = 0;
        Object comp;
        try {
            comp = model.component(ctag);
        } catch (Throwable t) {
            try {
                comp = model.modelNode(ctag);
            } catch (Throwable t2) {
                return 0;
            }
        }

        String[] varTags;
        try {
            Object vlist = comp.getClass().getMethod("variable").invoke(comp);
            varTags = (String[]) vlist.getClass().getMethod("tags")
                .invoke(vlist);
        } catch (Throwable t) {
            return 0;
        }

        for (String vTag : varTags) {
            Object vcoll;
            try {
                vcoll = model.variable(vTag);
            } catch (Throwable t) {
                continue;
            }

            // Enumerate this collection's variable names.
            String[] varNames;
            try {
                varNames = (String[]) vcoll.getClass().getMethod("varnames")
                    .invoke(vcoll);
            } catch (Throwable t) {
                continue;
            }
            if (varNames == null) continue;

            for (String vName : varNames) {
                // Path A — try 1-arg methods taking the variable name.
                for (String mname : VAR_UNIT_METHODS_1ARG) {
                    String declared = tryReadStringMethod(
                        vcoll, mname, new Class<?>[]{String.class},
                        new Object[]{vName});
                    if (declared != null && !declared.isEmpty()) {
                        writeVarUnitRecord(
                            w, mphPath, comsolVersion, vName, sdim,
                            ctag + "/" + vTag, mname, declared);
                        count++;
                        // Don't break — a variable may have multiple
                        // declarations (unit AND customSourceTermUnit).
                    }
                }

                // Path B — try 2-arg getString-style.
                for (String key : VAR_UNIT_METHODS_2ARG_KEYS) {
                    String declared = tryReadStringMethod(
                        vcoll, "getString",
                        new Class<?>[]{String.class, String.class},
                        new Object[]{vName, key});
                    if (declared != null && !declared.isEmpty()) {
                        writeVarUnitRecord(
                            w, mphPath, comsolVersion, vName, sdim,
                            ctag + "/" + vTag, key, declared);
                        count++;
                    }
                }
            }

            // Path C — legacy: collection-level properties that encode
            // a shared unit for all variables in the collection.
            String[] propNames;
            try {
                propNames = (String[]) vcoll.getClass().getMethod("properties")
                    .invoke(vcoll);
            } catch (Throwable t) {
                propNames = null;
            }
            if (propNames != null) {
                for (String pname : propNames) {
                    if (!UNIT_DECL_KEY.matcher(pname).find()) continue;
                    String val = readStringProperty(vcoll, pname);
                    if (val == null || val.isEmpty()) continue;
                    for (String vName : varNames) {
                        writeVarUnitRecord(
                            w, mphPath, comsolVersion, vName, sdim,
                            ctag + "/" + vTag, pname, val);
                        count++;
                    }
                }
            }
        }
        return count;
    }

    /** Reflectively call a String-returning method with given arg types.
     *  Returns null on any failure (method not found, bad args, null
     *  return). Used for the fallback-chain probes above. */
    private static String tryReadStringMethod(
            Object target, String method,
            Class<?>[] argTypes, Object[] args) {
        try {
            Method m = target.getClass().getMethod(method, argTypes);
            Object r = m.invoke(target, args);
            return r == null ? null : r.toString();
        } catch (Throwable t) {
            return null;
        }
    }

    // ---- Expression classification ----

    static class ExprFilter {
        String source; // "regex_heuristic"
    }

    private static ExprFilter classifyExpression(String expr) {
        if (expr == null) return null;
        String trimmed = expr.trim();
        if (trimmed.isEmpty()) return null;
        if (BOOLEAN_VALUES.matcher(trimmed).matches()) return null;
        // Pure integer or decimal number (no unit, no operator, no ident)
        if (trimmed.matches("^[-+]?\\d+(\\.\\d+)?([eE][-+]?\\d+)?$")) {
            return null;
        }
        if (!EXPRESSION_HEURISTIC.matcher(trimmed).find()) {
            // Could still be a bare identifier referring to a param —
            // treat those as expressions since they carry a deducible unit.
            if (!trimmed.matches("^[A-Za-z_][A-Za-z_0-9]*$")) return null;
        }
        ExprFilter f = new ExprFilter();
        f.source = "regex_heuristic";
        return f;
    }

    private static String readStringProperty(Object target, String name) {
        try {
            Method m = target.getClass().getMethod("getString", String.class);
            Object o = m.invoke(target, name);
            return o == null ? null : o.toString();
        } catch (Throwable ignore) {
        }
        try {
            Method m = target.getClass().getMethod("get", String.class);
            Object o = m.invoke(target, name);
            return o == null ? null : o.toString();
        } catch (Throwable ignore) {
        }
        return null;
    }

    private static String bestInterfaceName(Object obj) {
        Class<?>[] ifs = obj.getClass().getInterfaces();
        if (ifs.length > 0) return ifs[0].getSimpleName();
        return obj.getClass().getSimpleName();
    }

    // ---- Writers ----

    // Bump when the slot-record schema changes in an incompatible way.
    // The aggregator reads this field and skips records whose version
    // it does not recognise, preventing silent ingestion of stale dumps.
    private static final int SCHEMA_VERSION = 1;

    private static void writeSlotRecord(
            BufferedWriter w, String mphPath, String comsolVersion,
            String componentTag,
            String physicsTag, String physicsType, String physicsTypeSource,
            String sdim, String featureTag, String featureType,
            String featureTypeSource, String scope,
            String slotProperty, String expression, ExprFilter filter)
            throws IOException {
        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"kind\":\"slot\"");
        sb.append(",\"schema_version\":").append(SCHEMA_VERSION);
        sb.append(",\"model_path\":\"").append(escape(mphPath)).append("\"");
        sb.append(",\"comsol_version\":\"").append(escape(comsolVersion)).append("\"");
        sb.append(",\"component_tag\":\"").append(escape(componentTag)).append("\"");
        sb.append(",\"physics_tag\":\"").append(escape(physicsTag)).append("\"");
        sb.append(",\"physics_type\":\"").append(escape(physicsType)).append("\"");
        sb.append(",\"type_source\":\"").append(escape(physicsTypeSource)).append("\"");
        sb.append(",\"sdim\":\"").append(escape(sdim)).append("\"");
        sb.append(",\"feature_tag\":\"").append(escape(featureTag)).append("\"");
        sb.append(",\"feature_type\":\"").append(escape(featureType)).append("\"");
        sb.append(",\"feature_type_source\":\"").append(escape(featureTypeSource)).append("\"");
        sb.append(",\"feature_scope\":\"").append(escape(scope)).append("\"");
        sb.append(",\"slot_property\":\"").append(escape(slotProperty)).append("\"");
        sb.append(",\"expression\":\"").append(escape(expression)).append("\"");
        sb.append(",\"filter_source\":\"").append(escape(filter.source)).append("\"");
        sb.append("}\n");
        w.write(sb.toString());
    }

    private static void writeVarUnitRecord(
            BufferedWriter w, String mphPath, String comsolVersion,
            String varName, String sdim, String scope,
            String declarationKey, String declaredUnit) throws IOException {
        StringBuilder sb = new StringBuilder(128);
        sb.append("{\"kind\":\"var_unit\"");
        sb.append(",\"schema_version\":").append(SCHEMA_VERSION);
        sb.append(",\"model_path\":\"").append(escape(mphPath)).append("\"");
        sb.append(",\"comsol_version\":\"").append(escape(comsolVersion)).append("\"");
        sb.append(",\"var_name\":\"").append(escape(varName)).append("\"");
        sb.append(",\"sdim\":\"").append(escape(sdim)).append("\"");
        sb.append(",\"scope\":\"").append(escape(scope)).append("\"");
        sb.append(",\"declaration_key\":\"").append(escape(declarationKey)).append("\"");
        sb.append(",\"declared_unit\":\"").append(escape(declaredUnit)).append("\"");
        sb.append("}\n");
        w.write(sb.toString());
    }

    // ---- Helpers ----

    private interface StrSupplier { String get() throws Exception; }
    private interface ArrSupplier { String[] get() throws Exception; }

    private static String safeString(StrSupplier s) {
        try { String r = s.get(); return r == null ? "unknown" : r; }
        catch (Exception e) { return "unknown"; }
    }

    private static String safeStringOrNull(StrSupplier s) {
        try { return s.get(); }
        catch (Exception e) { return null; }
    }

    private static String[] safeStringArray(ArrSupplier s) {
        try { String[] r = s.get(); return r == null ? new String[0] : r; }
        catch (Exception e) { return new String[0]; }
    }

    private static void emit(String json) {
        System.out.println(json);
        System.out.flush();
    }

    private static void emitProgress(int idx, int total, String mph) {
        StringBuilder sb = new StringBuilder(128);
        sb.append("{\"progress\":true");
        sb.append(",\"index\":").append(idx);
        sb.append(",\"total\":").append(total);
        sb.append(",\"mph\":\"").append(escape(mph)).append("\"");
        sb.append("}");
        emit(sb.toString());
    }

    private static void emitStderr(String msg) {
        System.err.println(msg);
        System.err.flush();
    }

    private static void emitStderrLoad(String mph, Throwable e) {
        String m = e.getMessage() != null
            ? e.getMessage()
            : e.getClass().getName();
        emitStderr("LOAD_ERROR " + mph + ": " + m);
    }

    private static String escape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '\t': sb.append("\\t");  break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.toString();
    }
}
