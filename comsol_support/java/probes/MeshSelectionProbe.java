/*
 * MeshSelectionProbe — feature + CHILD-feature selection semantics.
 * READ-ONLY. Never saves, never runs the mesh.
 *
 * Contract: public static Map<String,Object> query(Model m, Map<String,String> args)
 *
 * WHY THIS EXISTS: enumerating a mesh sequence's features and their
 * properties is NOT enough — child features (e.g. a Sweep's
 * Distribution) carry their OWN selections, and that is only visible by
 * calling selection() on each child (G-DISTRIBUTION-SCOPABLE-CHILD was
 * discovered exactly this way). This probe walks every feature AND
 * child, reads each selection every way the 6.4 API offers (each
 * attempt isolated, throw text captured verbatim), resolves a named
 * sweep/Distribution pair explicitly, retries the sweep's
 * sourceface/targetface through both accessors
 * (G-SWEPT-SOURCEFACE-READBACK: absence is only reportable after both
 * fail), optionally runs an in-tool sweep-layer census, and always ends
 * with a residual-domain census so every output carries its own
 * coverage state.
 *
 * Args (all optional):
 *   comp        component (default: first component)
 *   mesh        mesh sequence (default: first mesh of the component)
 *   maxent      full entity list emitted when selection <= this (default 200)
 *   sweeptag    sweep feature for parts B/C (default swe1; degrades cleanly)
 *   distag      Distribution child for part B (default dis1)
 *   censusdoms  comma-separated domain ids for the in-tool layer census
 *               (default: empty = census skipped)
 *   axis        sweep axis for the census, 0/1/2 = x/y/z (default 2)
 *   label       free-form tag echoed into the output for provenance
 *
 * Origin: a large-assembly meshing campaign — promoted into comsol-support
 * (layer census made opt-in; tags auto-detected).
 * See docs/meshing.md §5.
 */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.lang.reflect.Method;
import java.util.*;

public class MeshSelectionProbe {

    static final String[] VOL_TYPES = {"tet", "hex", "prism", "pyr"};

    public static Map<String,Object> query(Model m, Map<String,String> args) {
        Map<String,Object> out = new LinkedHashMap<>();
        List<String> notes = new ArrayList<>();
        out.put("notes", notes);
        if (args == null) args = new HashMap<>();
        String comp = args.getOrDefault("comp", "");
        if (comp.isEmpty()) {
            try { String[] ct = m.component().tags(); if (ct.length > 0) comp = ct[0]; } catch (Throwable t) {}
        }
        if (comp.isEmpty()) comp = "comp1";
        String meshTag = args.getOrDefault("mesh", "");
        if (meshTag.isEmpty()) {
            try { String[] mt = m.component(comp).mesh().tags(); if (mt.length > 0) meshTag = mt[0]; } catch (Throwable t) {}
        }
        if (meshTag.isEmpty()) meshTag = "mesh1";
        int maxent = Integer.parseInt(args.getOrDefault("maxent", "200"));
        int axis   = Integer.parseInt(args.getOrDefault("axis", "2"));
        out.put("label", args.getOrDefault("label", ""));

        // Provenance stamp (one-COMSOL-JVM discipline — docs/meshing.md §6).
        Map<String,Object> excl = new LinkedHashMap<>();
        excl.put("epoch_ms", System.currentTimeMillis());
        excl.put("jvm", java.lang.management.ManagementFactory.getRuntimeMXBean().getName());
        out.put("jvm_stamp", excl);

        MeshSequence ms = m.component(comp).mesh(meshTag);
        String geomTag = "geom1";
        try { geomTag = m.component(comp).geom().tags()[0]; } catch (Throwable t) {}
        out.put("component", comp); out.put("geom", geomTag); out.put("mesh", meshTag);

        String[] tags = new String[0];
        try { tags = ms.feature().tags(); } catch (Throwable t) { notes.add("feature().tags(): " + sv(t)); }
        out.put("feature_order", String.join(",", tags));

        // ===== PART A : every feature and child, with its selection =====
        List<Map<String,Object>> feats = new ArrayList<>();
        out.put("part_A_features", feats);
        for (String tg : tags) {
            MeshFeature f = null;
            try { f = ms.feature(tg); } catch (Throwable t) { notes.add("feature(" + tg + "): " + sv(t)); }
            Map<String,Object> row = describeFeature(tg, f, null, maxent);
            List<Map<String,Object>> kids = new ArrayList<>();
            String[] ktags = new String[0];
            try { ktags = f.feature().tags(); } catch (Throwable t) { row.put("children_error", sv(t)); }
            for (String kt : ktags) {
                MeshFeature kf = null;
                try { kf = f.feature(kt); } catch (Throwable t) {}
                kids.add(describeFeature(kt, kf, tg, maxent));
            }
            row.put("children", kids);
            feats.add(row);
        }

        // ===== PART B : a named Distribution child, resolved explicitly =====
        Map<String,Object> pb = new LinkedHashMap<>();
        out.put("part_B_distribution", pb);
        String sweepTag = args.getOrDefault("sweeptag", "swe1");
        String disTag   = args.getOrDefault("distag", "dis1");
        pb.put("sweep_tag", sweepTag); pb.put("dis_tag", disTag);
        MeshFeature dis = null;
        try { dis = ms.feature(sweepTag).feature(disTag); pb.put("resolved_as", sweepTag + "/" + disTag); }
        catch (Throwable t) { pb.put("resolve_error", sv(t)); }
        if (dis != null) {
            Map<String,Object> props = new LinkedHashMap<>();
            for (String p : new String[]{"numelem","type","elemratio","reverse","symmetric","method","growthrate"}) {
                try { props.put(p, String.valueOf(dis.getString(p))); }
                catch (Throwable t) { props.put(p + "__throw", sv(t)); }
            }
            pb.put("properties", props);
            pb.put("selection", describeSelection(dis, maxent));
            try { pb.put("all_property_keys", Arrays.toString((String[]) invoke(dis, "properties"))); }
            catch (Throwable t) { pb.put("all_property_keys__throw", sv(t)); }
        }

        // ===== PART C : sweep source / target face readback =====
        Map<String,Object> pc = new LinkedHashMap<>();
        out.put("part_C_sweep_faces", pc);
        MeshFeature swe = null;
        try { swe = ms.feature(sweepTag); } catch (Throwable t) { pc.put("resolve_error", sv(t)); }
        if (swe != null) {
            for (String key : new String[]{"sourceface", "targetface", "srcface", "dstface"}) {
                try { pc.put(key + "__getString", String.valueOf(swe.getString(key))); }
                catch (Throwable t) { pc.put(key + "__getString_throw", sv(t)); }
                try {
                    Object sel = invoke(swe, "selection", key);
                    Map<String,Object> s = new LinkedHashMap<>();
                    for (int d = 0; d <= 3; d++) {
                        try { int[] e = (int[]) invoke(sel, "entities", d);
                              s.put("entities_dim" + d + "_len", e == null ? -1 : e.length); }
                        catch (Throwable t) { s.put("entities_dim" + d + "__throw", sv(t)); }
                    }
                    try { s.put("dimension", String.valueOf(invoke(sel, "dimension"))); }
                    catch (Throwable t) { s.put("dimension__throw", sv(t)); }
                    pc.put(key + "__selection", s);
                } catch (Throwable t) { pc.put(key + "__selection_throw", sv(t)); }
            }
            try { pc.put("sweep_all_property_keys", Arrays.toString((String[]) invoke(swe, "properties"))); }
            catch (Throwable t) { pc.put("sweep_all_property_keys__throw", sv(t)); }
            try { pc.put("sweep_buildtime", String.valueOf(swe.getString("buildtime"))); } catch (Throwable t) {}
            try { pc.put("sweep_buildoutput", String.valueOf(swe.getString("buildoutput"))); } catch (Throwable t) {}
        }

        // ===== PART D : in-tool sweep-layer census (opt-in) =====
        String censusArg = args.getOrDefault("censusdoms", "").trim();
        if (!censusArg.isEmpty()) {
            Map<String,Object> pd = new LinkedHashMap<>();
            out.put("part_D_layer_census", pd);
            pd.put("axis_index", axis);
            pd.put("axis_name", axis == 0 ? "x" : axis == 1 ? "y" : "z");
            pd.put("method", "per-domain hexahedra; element centroid projected on the sweep axis; "
                 + "distinct layers = count of centroid clusters separated by > 0.25 * median "
                 + "element extent along the axis. Answers 'did numelem actually change the "
                 + "layer count' in-tool (G-DISTRIBUTION-NUMELEM-IS-REQUEST).");
            List<Integer> cdoms = new ArrayList<>();
            for (String s : censusArg.split(",")) {
                s = s.trim(); if (!s.isEmpty()) cdoms.add(Integer.parseInt(s));
            }
            try {
                double[][] v = ms.getVertex();
                boolean vT = (v.length == 3 && v[0].length > 3);
                int nNode = vT ? v[0].length : v.length;
                pd.put("n_nodes", nNode);
                pd.put("vertex_layout", vT ? "[3][n]" : "[n][3]");

                int[][] hx = (int[][]) invoke(ms, "getElem", "hex");
                int[] hent = (int[]) invoke(ms, "getElemEntity", "hex");
                int nHex = hent.length;
                boolean eT = (hx.length <= 27 && hx[0].length == nHex);
                int nn = eT ? hx.length : hx[0].length;
                pd.put("n_hex", nHex);
                pd.put("hex_layout", eT ? "[nn][nElem]" : "[nElem][nn]");
                pd.put("nodes_per_hex", nn);

                List<Map<String,Object>> rows = new ArrayList<>();
                for (int d : cdoms) {
                    Map<String,Object> r = new LinkedHashMap<>();
                    r.put("dom", d);
                    List<double[]> cz = new ArrayList<>();     // {centroid, extent}
                    for (int i = 0; i < nHex; i++) {
                        if (hent[i] != d) continue;
                        double sum = 0, mn = Double.MAX_VALUE, mx = -Double.MAX_VALUE;
                        for (int k = 0; k < nn; k++) {
                            int node = eT ? hx[k][i] : hx[i][k];
                            double z = vT ? v[axis][node] : v[node][axis];
                            sum += z; if (z < mn) mn = z; if (z > mx) mx = z;
                        }
                        cz.add(new double[]{sum / nn, mx - mn});
                    }
                    r.put("n_hex_in_domain", cz.size());
                    if (cz.isEmpty()) { r.put("layers", 0); r.put("note", "no hexahedra in this domain"); rows.add(r); continue; }
                    double[] ext = new double[cz.size()];
                    for (int i = 0; i < cz.size(); i++) ext[i] = cz.get(i)[1];
                    Arrays.sort(ext);
                    double medExt = ext[ext.length / 2];
                    double[] cen = new double[cz.size()];
                    for (int i = 0; i < cz.size(); i++) cen[i] = cz.get(i)[0];
                    Arrays.sort(cen);
                    double tol = Math.max(0.25 * medExt, 1e-12);
                    int layers = 1;
                    for (int i = 1; i < cen.length; i++) if (cen[i] - cen[i-1] > tol) layers++;
                    r.put("layers", layers);
                    r.put("median_elem_extent_on_axis", medExt);
                    r.put("min_elem_extent_on_axis", ext[0]);
                    r.put("max_elem_extent_on_axis", ext[ext.length-1]);
                    r.put("axis_span_of_domain", cen[cen.length-1] - cen[0] + medExt);
                    r.put("cluster_tol", tol);
                    r.put("hex_per_layer", (double) cz.size() / layers);
                    rows.add(r);
                }
                pd.put("domains", rows);
            } catch (Throwable t) { pd.put("census_error", sv(t)); }
        }

        // ===== PART E : per-type quality / volume, measure labeled =====
        Map<String,Object> pe = new LinkedHashMap<>();
        out.put("part_E_stats", pe);
        try {
            MeshStatistics st = ms.stat();
            try { pe.put("quality_measure", String.valueOf(invoke(st, "getQualityMeasure"))); } catch (Throwable t) {}
            for (String ty : new String[]{"tet","hex","prism","pyr","tri","quad"}) {
                int c = 0; try { c = ms.getNumElem(ty); } catch (Throwable t) {}
                Map<String,Object> tr = new LinkedHashMap<>();
                tr.put("n", c);
                if (c > 0) {
                    try { tr.put("min_quality", ((Number) invoke(st, "getMinQuality", ty)).doubleValue()); }
                    catch (Throwable t) { tr.put("min_quality__throw", sv(t)); }
                    try { tr.put("min_volume", ((Number) invoke(st, "getMinVolume", ty)).doubleValue()); }
                    catch (Throwable t) { tr.put("min_volume__throw", sv(t)); }
                }
                pe.put(ty, tr);
            }
        } catch (Throwable t) { pe.put("stats_error", sv(t)); }

        // Residual census, so every output file carries its own coverage.
        int nDom = 0;
        try { nDom = ((Number) invoke(m.component(comp).geom(geomTag), "getNDomains")).intValue(); } catch (Throwable t) {}
        out.put("nDomains", nDom);
        try {
            int[] cnt = new int[nDom + 1];
            for (String ty : VOL_TYPES) {
                try { int[] e = (int[]) invoke(ms, "getElemEntity", ty);
                      if (e != null) for (int x : e) if (x >= 1 && x <= nDom) cnt[x]++; } catch (Throwable t) {}
            }
            List<Integer> res = new ArrayList<>();
            for (int d = 1; d <= nDom; d++) if (cnt[d] == 0) res.add(d);
            out.put("residual_count", res.size());
            out.put("residual_domains", res.size() <= 800 ? res : res.subList(0, 800));
        } catch (Throwable t) { notes.add("residual census: " + sv(t)); }

        return out;
    }

    // ---------------------------------------------------------------- helpers
    static Map<String,Object> describeFeature(String tag, MeshFeature f, String parent, int maxent) {
        Map<String,Object> row = new LinkedHashMap<>();
        row.put("tag", tag);
        if (parent != null) row.put("parent", parent);
        if (f == null) { row.put("error", "feature could not be resolved"); return row; }
        try { row.put("type", String.valueOf(invoke(f, "getType"))); }
        catch (Throwable t) { row.put("type__throw", sv(t)); }
        try { row.put("property_keys", Arrays.toString((String[]) invoke(f, "properties"))); }
        catch (Throwable t) { row.put("property_keys__throw", sv(t)); }
        row.put("selection", describeSelection(f, maxent));
        return row;
    }

    /** Read a feature's selection every way the 6.4 API offers, each attempt isolated. */
    static Map<String,Object> describeSelection(MeshFeature f, int maxent) {
        Map<String,Object> s = new LinkedHashMap<>();
        Object sel = null;
        try { sel = invoke(f, "selection"); s.put("has_selection", sel != null); }
        catch (Throwable t) { s.put("selection__throw", sv(t)); return s; }
        if (sel == null) return s;
        s.put("selection_class", sel.getClass().getName());
        try { s.put("geom", String.valueOf(invoke(sel, "geom"))); }
        catch (Throwable t) { s.put("geom__throw", sv(t)); }
        Integer dim = null;
        try { Object d = invoke(sel, "dimension"); s.put("dimension", String.valueOf(d));
              if (d instanceof Number) dim = ((Number) d).intValue(); }
        catch (Throwable t) { s.put("dimension__throw", sv(t)); }

        for (String meth : new String[]{"isRemaining", "isAll", "named", "getEntryKeys", "inputEntities"}) {
            try {
                Object r = invoke(sel, meth);
                s.put(meth, r == null ? "null"
                     : r instanceof Object[] ? Arrays.toString((Object[]) r)
                     : r instanceof int[] ? ("int[" + ((int[]) r).length + "]")
                     : String.valueOf(r));
            } catch (Throwable t) { s.put(meth + "__throw", sv(t)); }
        }
        for (String key : new String[]{"selectionmode", "seltype", "entitydim"}) {
            try { s.put("getString_" + key, String.valueOf(invoke(sel, "getString", key))); }
            catch (Throwable t) { s.put("getString_" + key + "__throw", sv(t)); }
        }

        Map<String,Object> ents = new LinkedHashMap<>();
        for (int d = 0; d <= 3; d++) {
            try {
                int[] e = (int[]) invoke(sel, "entities", d);
                if (e == null) { ents.put("dim" + d, "null"); continue; }
                Map<String,Object> er = new LinkedHashMap<>();
                er.put("n", e.length);
                if (e.length > 0) {
                    er.put("first20", Arrays.toString(Arrays.copyOfRange(e, 0, Math.min(20, e.length))));
                    er.put("last20", Arrays.toString(Arrays.copyOfRange(e, Math.max(0, e.length-20), e.length)));
                    if (e.length <= maxent) { List<Integer> full = new ArrayList<>();
                        for (int x : e) full.add(x); er.put("full", full); }
                    else er.put("full", "omitted: " + e.length + " > maxent");
                }
                ents.put("dim" + d, er);
            } catch (Throwable t) { ents.put("dim" + d + "__throw", sv(t)); }
        }
        s.put("entities", ents);
        if (dim != null) s.put("declared_dimension", dim);
        return s;
    }

    /** Throw text via String.valueOf(t), never getMessage() (which is
     *  often the useless aggregate on COMSOL exceptions). */
    static String sv(Throwable t) {
        StringBuilder sb = new StringBuilder();
        Throwable r = t; int guard = 0;
        while (r != null && guard++ < 8) {
            if (sb.length() > 0) sb.append(" <- ");
            sb.append(String.valueOf(r));
            if (r.getCause() == r) break;
            r = r.getCause();
        }
        String s = sb.toString();
        return s.length() > 1200 ? s.substring(0, 1200) : s;
    }

    static Object invoke(Object o, String method, Object... a) throws Exception {
        if (o == null) return null;
        if (a.length == 0) return o.getClass().getMethod(method).invoke(o);
        Exception last = null;
        for (Method me : o.getClass().getMethods()) {
            if (!me.getName().equals(method) || me.getParameterCount() != a.length) continue;
            try { return me.invoke(o, a); } catch (IllegalArgumentException e) { last = e; }
        }
        if (last != null) throw last;
        throw new NoSuchMethodException(method + "/" + a.length);
    }
}
