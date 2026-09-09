/*
 * MeshStatsProbe — read-only mesh-statistics + unmeshed-domain census.
 *
 * Contract: public static Map<String,Object> query(Model m, Map<String,String> args)
 *   Run via query-mph on a meshed .mph (never saves).
 *
 * Returns:
 *   - element counts by type (tet/hex/prism/pyr/tri/quad/edg/vtx)
 *   - min/avg quality under the DEFAULT measure, labeled with which
 *     measure that is (volcircum unless changed —
 *     G-MESHSTATS-DEFAULT-VOLCIRCUM)
 *   - unmeshed-domain census via getElemEntity (domain ids are 1-based —
 *     G-GETADJ-ROW-ZERO-EXTERIOR): count + identity list
 *   - volume extremes incl. min_volume_by_type (straight-edge measure —
 *     G-GETMINVOLUME-STRAIGHT-EDGE)
 *   - optional full measure×type quality sweep (measures=on) and
 *     per-domain inverted-element localization (localize=on)
 *   - hasProblems/problemNames on the mesh and each feature (unreliable
 *     as a failure census — see G-MESH-BUILDINFO-SURVIVES-THROW — but
 *     reported for comparison), plus a MeshSequence API dump
 *
 * CHEAP BY DEFAULT (campaign finding F-D10-11: the full sweep held the
 * exclusive COMSOL JVM slot for 86 minutes): the measure sweep and the
 * localization pass are OFF unless requested.
 *
 * Args (all optional):
 *   comp=<tag>       component (default: first component)
 *   mesh=<tag>       mesh sequence (default: first mesh of the component)
 *   measures=on      run the 9-measure × per-type quality sweep (default off)
 *   localize=on      per-domain inverted-element scan (default off)
 *   localize_budget_ms=N   time budget for the scan (default 240000)
 *
 * All reflective/defensive so the (expensive) model load is never wasted
 * on one bad API guess; missing methods land in "notes", never thrown.
 *
 * Origin: a large-assembly meshing campaign — promoted into
 * comsol-support with cheap defaults and the localization
 * loop fixed to set its quality measure explicitly (the campaign
 * version localized under the restored default measure and mislabelled
 * 41 domains for several cycles). See docs/meshing.md §4–5.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.lang.reflect.Method;
import java.util.*;

public class MeshStatsProbe {

    static final String[] VOL_TYPES = {"tet", "hex", "prism", "pyr"};
    static final String[] SURF_TYPES = {"tri", "quad"};
    static final String[] LINE_TYPES = {"edg", "vtx"};

    public static Map<String,Object> query(Model m, Map<String,String> args) {
        Map<String,Object> out = new LinkedHashMap<>();
        List<String> notes = new ArrayList<>();
        out.put("notes", notes);

        String comp = args != null ? args.getOrDefault("comp", "") : "";
        if (comp.isEmpty()) {
            try { String[] ct = m.component().tags(); if (ct.length > 0) comp = ct[0]; } catch (Throwable t) {}
        }
        if (comp.isEmpty()) comp = "comp1";
        out.put("component", comp);
        String meshTag = args != null ? args.getOrDefault("mesh", "") : "";
        if (meshTag.isEmpty()) {
            try { String[] mt = m.component(comp).mesh().tags(); if (mt.length > 0) meshTag = mt[0]; } catch (Throwable t) {}
        }
        if (meshTag.isEmpty()) meshTag = "mesh1";
        out.put("mesh_tag", meshTag);

        MeshSequence ms = null;
        try { ms = m.component(comp).mesh(meshTag); } catch (Throwable t) { notes.add("mesh fetch failed: " + t); return out; }

        // ---- API discovery: dump stat-ish MeshSequence method signatures ----
        List<String> apiMethods = new ArrayList<>();
        for (Method me : ms.getClass().getMethods()) {
            String n = me.getName().toLowerCase();
            if (n.contains("elem") || n.contains("stat") || n.contains("qual")
                    || n.contains("vertex") || n.contains("num") || n.contains("mesh")
                    || n.contains("problem")) {
                StringBuilder sig = new StringBuilder(me.getName() + "(");
                Class<?>[] ps = me.getParameterTypes();
                for (int i = 0; i < ps.length; i++) { if (i > 0) sig.append(","); sig.append(ps[i].getSimpleName()); }
                sig.append(")->").append(me.getReturnType().getSimpleName());
                apiMethods.add(sig.toString());
            }
        }
        Collections.sort(apiMethods);
        out.put("meshseq_api_methods", apiMethods);

        // ---- total geometry domain count (unmeshed census denominator) ----
        int nDom = 0;
        String geomTag = "geom1";
        try {
            geomTag = m.component(comp).geom().tags()[0];
            GeomSequence g = m.component(comp).geom(geomTag);
            Object v = invoke(g, "getNDomains");
            if (v != null) nDom = ((Number) v).intValue();
        } catch (Throwable t) { notes.add("nDomains fetch failed: " + t); }
        out.put("nDomains", nDom);

        // ---- element counts by type ----
        Map<String,Object> counts = new LinkedHashMap<>();
        long totalVol = 0;
        for (String ty : concat(VOL_TYPES, SURF_TYPES, LINE_TYPES)) {
            int c = getNumElem(ms, ty, notes);
            counts.put(ty, c);
            if (isIn(ty, VOL_TYPES) && c > 0) totalVol += c;
        }
        out.put("element_counts_by_type", counts);
        out.put("total_volumetric_elements", totalVol);
        try { Object v = invoke(ms, "getNumElem"); if (v != null) out.put("total_elements_all", ((Number) v).intValue()); }
        catch (Throwable t) { notes.add("getNumElem() no-arg unavailable: " + t); }

        // ---- quality under the DEFAULT measure (labeled below) ----
        out.put("min_quality", tryDouble(ms, "getMinQuality"));
        out.put("avg_quality", tryDouble(ms, "getMeanQuality"));
        Map<String,Object> qByType = new LinkedHashMap<>();
        for (String ty : concat(VOL_TYPES, SURF_TYPES)) {
            if (getNumElem(ms, ty, notes) <= 0) continue;
            Map<String,Object> row = new LinkedHashMap<>();
            try { row.put("min", ((Number) invoke(ms, "getMinQuality", ty)).doubleValue()); } catch (Throwable t) {}
            try { row.put("avg", ((Number) invoke(ms, "getMeanQuality", ty)).doubleValue()); } catch (Throwable t) {}
            if (!row.isEmpty()) qByType.put(ty, row);
        }
        out.put("quality_by_type", qByType);
        try { Object h = invoke(ms, "getQualityDistr", 10); if (h instanceof int[]) out.put("quality_hist10", (int[]) h); }
        catch (Throwable t) { notes.add("getQualityDistr(10) unavailable: " + t); }
        out.put("second_order_elements", tryBool(ms, "hasSecondOrderElements"));

        // ---- quality MEASURE handling (G-MESHSTATS-DEFAULT-VOLCIRCUM) ----
        // getMinQuality(String) takes an element TYPE, not a measure name.
        // The measure selector lives on MeshStatistics (ms.stat()):
        // getQualityMeasure()/setQualityMeasure(String). Label every number.
        Object st = null;
        try { st = invoke(ms, "stat"); } catch (Throwable t) { notes.add("ms.stat(): " + rootMsg(t)); }
        String measure0 = null;
        try { measure0 = String.valueOf(invoke(st, "getQualityMeasure")); } catch (Throwable t) {}
        out.put("quality_measure_default", measure0);
        out.put("quality_measure_note",
                "min_quality/avg_quality/quality_by_type above are reported under the measure named in "
              + "quality_measure_default (volcircum unless changed); pass measures=on for the full sweep.");

        Map<String,Object> qByMeasure = new LinkedHashMap<>();
        List<String> rejected = new ArrayList<>();
        String[] measCands = {"skewness","maxangle","volcircum","vollength","condition","growth",
                              "curvedskewness","curvedvolcircum","curvedmaxangle"};
        // EXPENSIVE: re-derives quality over the whole mesh per (measure,
        // type) pair — ~10 min on a 300k-element mesh. Off by default.
        boolean doMeasures = "on".equalsIgnoreCase(args == null ? "off" : args.getOrDefault("measures", "off"));
        out.put("quality_measure_sweep_run", doMeasures);
        if (st != null && doMeasures) {
            for (String meas : measCands) {
                Map<String,Object> row = new LinkedHashMap<>();
                try {
                    invoke(st, "setQualityMeasure", meas);
                    String got = String.valueOf(invoke(st, "getQualityMeasure"));
                    row.put("accepted_as", got);
                    if (!meas.equals(got)) row.put("alias_note", "requested " + meas + ", API reports " + got);
                    try { row.put("min",  ((Number) invoke(st, "getMinQuality")).doubleValue()); } catch (Throwable t) {}
                    try { row.put("mean", ((Number) invoke(st, "getMeanQuality")).doubleValue()); } catch (Throwable t) {}
                    Map<String,Object> byType = new LinkedHashMap<>();
                    for (String ty : concat(VOL_TYPES, SURF_TYPES)) {
                        if (getNumElem(ms, ty, notes) <= 0) continue;
                        Map<String,Object> tr = new LinkedHashMap<>();
                        try { tr.put("min",  ((Number) invoke(st, "getMinQuality",  ty)).doubleValue()); } catch (Throwable t) {}
                        try { tr.put("mean", ((Number) invoke(st, "getMeanQuality", ty)).doubleValue()); } catch (Throwable t) {}
                        if (!tr.isEmpty()) byType.put(ty, tr);
                    }
                    row.put("by_type", byType);
                    try { Object h = invoke(st, "getQualityDistr", 20); if (h instanceof int[]) row.put("hist20", (int[]) h); } catch (Throwable t) {}
                    qByMeasure.put(meas, row);
                } catch (Throwable t) {
                    rejected.add(meas + " :: " + rootMsg(t));
                }
            }
            try { if (measure0 != null && !"null".equals(measure0)) invoke(st, "setQualityMeasure", measure0); } catch (Throwable t) {}
        }
        out.put("quality_by_measure", qByMeasure);
        out.put("quality_measures_rejected", rejected);

        // ---- volume extremes (negative min volume == straight-edge inversion) ----
        Map<String,Object> volStats = new LinkedHashMap<>();
        try { volStats.put("min_volume", ((Number) invoke(st, "getMinVolume")).doubleValue()); } catch (Throwable t) {}
        try { volStats.put("max_volume", ((Number) invoke(st, "getMaxVolume")).doubleValue()); } catch (Throwable t) {}
        try { volStats.put("total_volume", ((Number) invoke(st, "getVolume")).doubleValue()); } catch (Throwable t) {}
        try { volStats.put("mean_growth_rate", ((Number) invoke(st, "getMeanGrowthRate")).doubleValue()); } catch (Throwable t) {}
        try { volStats.put("max_growth_rate", ((Number) invoke(st, "getMaxGrowthRate")).doubleValue()); } catch (Throwable t) {}
        Map<String,Object> minVolByType = new LinkedHashMap<>();
        for (String ty : VOL_TYPES) {
            if (getNumElem(ms, ty, notes) <= 0) continue;
            try { minVolByType.put(ty, ((Number) invoke(st, "getMinVolume", ty)).doubleValue()); } catch (Throwable t) {}
        }
        volStats.put("min_volume_by_type", minVolByType);
        out.put("volume_stats", volStats);

        // ---- unmeshed-domain census: domain -> #volumetric elements ----
        int[] domElemCount = new int[nDom + 1];   // 1-based domain indexing
        boolean mappingOk = false;
        for (String ty : VOL_TYPES) {
            if (getNumElem(ms, ty, notes) <= 0) continue;
            int[] ent = getElemEntity(ms, ty, 3, notes);
            if (ent == null) continue;
            mappingOk = true;
            for (int e : ent) { if (e >= 1 && e <= nDom) domElemCount[e]++; }
        }
        out.put("domain_element_mapping_ok", mappingOk);
        if (mappingOk && nDom > 0) {
            List<Integer> unmeshed = new ArrayList<>();
            for (int d = 1; d <= nDom; d++) if (domElemCount[d] == 0) unmeshed.add(d);
            out.put("unmeshed_domain_count", unmeshed.size());
            out.put("meshed_domain_count", nDom - unmeshed.size());
            if (unmeshed.size() <= 800) out.put("unmeshed_domains", unmeshed);
            else { out.put("unmeshed_domains_first800", unmeshed.subList(0, 800)); out.put("unmeshed_domains_truncated", true); }
        } else {
            notes.add("unmeshed census unavailable (mappingOk=" + mappingOk + ", nDom=" + nDom + ")");
        }

        // ---- localize inverted / non-positive-quality elements ----
        // Scopes MeshStatistics.selection() to one domain at a time. The
        // measure is set EXPLICITLY to skewness for the scan (the campaign
        // version scanned under the restored default measure and mislabelled
        // its results — G-MESHSTATS-DEFAULT-VOLCIRCUM applies inside loops
        // too). min_volume <= 0 flags straight-edge inversion regardless.
        boolean doLoc = "on".equalsIgnoreCase(args == null ? "off" : args.getOrDefault("localize", "off"));
        long locBudget = Long.parseLong(args == null ? "240000" : args.getOrDefault("localize_budget_ms", "240000"));
        if (doLoc && st != null && nDom > 0) {
            long tLoc = System.currentTimeMillis();
            String scanMeasure = null;
            try { invoke(st, "setQualityMeasure", "skewness");
                  scanMeasure = String.valueOf(invoke(st, "getQualityMeasure")); } catch (Throwable t) {}
            List<Map<String,Object>> bad = new ArrayList<>();
            int scanned = 0; boolean truncated = false;
            for (int d = 1; d <= nDom; d++) {
                if (System.currentTimeMillis() - tLoc > locBudget) { truncated = true; break; }
                if (domElemCount[d] == 0) continue;           // unmeshed: nothing to score
                try {
                    Object sel = invoke(st, "selection");
                    try { invoke(sel, "geom", geomTag, 3); } catch (Throwable t) { try { invoke(sel, "geom", 3); } catch (Throwable t2) {} }
                    try { invoke(sel, "set", new int[]{d}); } catch (Throwable t) { invoke(sel, "set", d); }
                    scanned++;
                    double mq = Double.NaN, mv = Double.NaN;
                    try { mq = ((Number) invoke(st, "getMinQuality")).doubleValue(); } catch (Throwable t) {}
                    try { mv = ((Number) invoke(st, "getMinVolume")).doubleValue(); } catch (Throwable t) {}
                    if ((!Double.isNaN(mq) && mq <= 0) || (!Double.isNaN(mv) && mv <= 0)) {
                        Map<String,Object> r = new LinkedHashMap<>();
                        r.put("dom", d); r.put("min_quality_skewness", mq); r.put("min_volume", mv);
                        r.put("n_elem", domElemCount[d]);
                        for (String ty : VOL_TYPES) {
                            try { Object v = invoke(st, "getNumElem", ty);
                                  int c = ((Number) v).intValue(); if (c > 0) r.put(ty, c); } catch (Throwable t) {}
                        }
                        bad.add(r);
                    }
                } catch (Throwable t) {
                    if (bad.size() == 0 && scanned == 0) { notes.add("localization unavailable: " + rootMsg(t)); break; }
                }
            }
            try { Object sel = invoke(st, "selection"); invoke(sel, "all"); } catch (Throwable t) {}
            try { if (measure0 != null && !"null".equals(measure0)) invoke(st, "setQualityMeasure", measure0); } catch (Throwable t) {}
            Map<String,Object> loc = new LinkedHashMap<>();
            loc.put("scan_measure", scanMeasure);
            loc.put("domains_scanned", scanned);
            loc.put("budget_exhausted", truncated);
            loc.put("wall_ms", System.currentTimeMillis() - tLoc);
            loc.put("n_domains_with_nonpositive", bad.size());
            loc.put("domains", bad.size() <= 400 ? bad : bad.subList(0, 400));
            out.put("inverted_element_localization", loc);
        }

        // ---- problems on mesh + each feature (comparison surface only) ----
        Map<String,Object> problems = new LinkedHashMap<>();
        problems.put("mesh_hasProblems", tryBool(ms, "hasProblems"));
        problems.put("mesh_problemNames", tryStringArray(ms, "problemNames"));
        problems.put("mesh_problems", tryStringArray(ms, "problems"));
        List<Map<String,Object>> feats = new ArrayList<>();
        try {
            for (String ft : ms.feature().tags()) {
                Map<String,Object> fr = new LinkedHashMap<>();
                fr.put("tag", ft);
                Object f = ms.feature(ft);
                try { fr.put("type", invoke(f, "getType")); } catch (Throwable t) {}
                fr.put("hasProblems", tryBool(f, "hasProblems"));
                fr.put("problemNames", tryStringArray(f, "problemNames"));
                feats.add(fr);
            }
        } catch (Throwable t) { notes.add("feature walk failed: " + t); }
        problems.put("features", feats);
        out.put("problems", problems);

        return out;
    }

    // ---- reflective stat getters (multiple candidate signatures) ----
    static int getNumElem(MeshSequence ms, String type, List<String> notes) {
        try { Object v = invoke(ms, "getNumElem", type); if (v != null) return ((Number) v).intValue(); }
        catch (Throwable t) { /* try alternates */ }
        for (String nm : new String[]{"getNumElements", "getNElem"}) {
            try { Object v = invoke(ms, nm, type); if (v != null) return ((Number) v).intValue(); } catch (Throwable t) {}
        }
        return 0;
    }
    static int[] getElemEntity(MeshSequence ms, String type, int dim, List<String> notes) {
        for (String nm : new String[]{"getElemEntity", "getElementEntity", "getElemDomain"}) {
            try { Object v = invoke(ms, nm, type, dim); int[] a = toI(v); if (a != null) return a; } catch (Throwable t) {}
            try { Object v = invoke(ms, nm, type); int[] a = toI(v); if (a != null) return a; } catch (Throwable t) {}
        }
        notes.add("getElemEntity unavailable for type=" + type);
        return null;
    }

    // ---- helpers ----
    static String rootMsg(Throwable t) {
        Throwable r = t;
        while (r.getCause() != null && r.getCause() != r) r = r.getCause();
        String s = r.getClass().getSimpleName() + ": " + String.valueOf(r.getMessage());
        return s.length() > 600 ? s.substring(0, 600) : s;
    }
    static Object invoke(Object o, String method, Object... a) throws Exception {
        if (o == null) return null;
        if (a.length == 0) return o.getClass().getMethod(method).invoke(o);
        for (Method me : o.getClass().getMethods()) {
            if (me.getName().equals(method) && me.getParameterCount() == a.length) {
                try { return me.invoke(o, a); } catch (IllegalArgumentException e) { /* next overload */ }
            }
        }
        Class<?>[] types = new Class[a.length];
        for (int i = 0; i < a.length; i++) types[i] = a[i].getClass();
        return o.getClass().getMethod(method, types).invoke(o, a);
    }
    static Boolean tryBool(Object o, String method) {
        try { Object v = invoke(o, method); return (v instanceof Boolean) ? (Boolean) v : null; } catch (Throwable t) { return null; }
    }
    static Double tryDouble(Object o, String method) {
        try { Object v = invoke(o, method); return (v instanceof Number) ? ((Number) v).doubleValue() : null; } catch (Throwable t) { return null; }
    }
    static Object tryStringArray(Object o, String method) {
        try { Object v = invoke(o, method); if (v instanceof String[]) return Arrays.asList((String[]) v); return v; }
        catch (Throwable t) { return null; }
    }
    static int[] toI(Object o) {
        if (o instanceof int[]) return (int[]) o;
        if (o instanceof int[][]) { int[][] a = (int[][]) o; if (a.length==1) return a[0]; }
        if (o instanceof Integer[]) { Integer[] a=(Integer[])o; int[] r=new int[a.length]; for(int i=0;i<a.length;i++) r[i]=a[i]; return r; }
        return null;
    }
    static boolean isIn(String s, String[] arr) { for (String a : arr) if (a.equals(s)) return true; return false; }
    static String[] concat(String[]... arrs) {
        List<String> l = new ArrayList<>();
        for (String[] a : arrs) l.addAll(Arrays.asList(a));
        return l.toArray(new String[0]);
    }
}
