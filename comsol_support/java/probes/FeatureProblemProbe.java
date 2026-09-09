/*
 * FeatureProblemProbe — per-feature mesh build records.
 * Contract: public static Map<String,Object> query(Model m, Map<String,String> args)
 *
 * Read-only. Harvests the PER-FEATURE build record that the aggregate
 * mesh exception hides. A failed whole-sequence build dies with one
 * useless aggregate string (`Multiple_problems_occurred_when_building_X`)
 * — COMSOL aggregating the stored problem records of ALL features. The
 * real, per-feature reason is kept on the feature itself: each mesh
 * feature carries buildtime / buildinfo / buildnote / builddetails /
 * buildoutput properties, and they SURVIVE the throw
 * (G-MESH-BUILDINFO-SURVIVES-THROW). Also reports each feature's
 * selection size and whether its selected domains actually meshed, so a
 * feature that "failed" but committed elements is not misread
 * (G-MESH-THROW-COMMITS-PARTIAL), plus the (unreliable — see gotcha)
 * hasProblems/problemNames surface for comparison.
 *
 * Args (all optional):
 *   comp=<tag>   component (default: first component)
 *   mesh=<tag>   mesh sequence (default: first mesh of the component)
 *
 * Origin: a 2,773-domain laminate meshing campaign —
 * promoted into comsol-support. See docs/meshing.md §5.
 */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.lang.reflect.Method;
import java.util.*;

public class FeatureProblemProbe {

    static final String[] VOL_TYPES = {"tet", "hex", "prism", "pyr"};
    static final String[] BUILD_PROPS = {"buildtime", "buildinfo",
            "buildnote", "builddetails", "buildoutput"};

    public static Map<String,Object> query(Model m, Map<String,String> args) {
        Map<String,Object> out = new LinkedHashMap<>();
        List<String> notes = new ArrayList<>();
        out.put("notes", notes);

        String comp = args != null ? args.getOrDefault("comp", "") : "";
        if (comp.isEmpty()) {
            try { String[] ct = m.component().tags();
                  if (ct.length > 0) comp = ct[0]; } catch (Throwable t) {}
        }
        if (comp.isEmpty()) comp = "comp1";
        String geomTag = "geom1";
        try { geomTag = m.component(comp).geom().tags()[0]; } catch (Throwable t) {}
        String meshTag = args != null ? args.getOrDefault("mesh", "") : "";
        if (meshTag.isEmpty()) {
            try { String[] mt = m.component(comp).mesh().tags();
                  if (mt.length > 0) meshTag = mt[0]; } catch (Throwable t) {}
        }
        if (meshTag.isEmpty()) meshTag = "mesh1";
        out.put("component", comp);
        out.put("mesh_tag", meshTag);
        MeshSequence ms = null;
        try { ms = m.component(comp).mesh(meshTag); }
        catch (Throwable t) { notes.add("mesh: " + t); return out; }

        int nDom = 0;
        try { nDom = ((Number) invoke(m.component(comp).geom(geomTag),
                "getNDomains")).intValue(); } catch (Throwable t) {}
        out.put("nDomains", nDom);

        // Per-domain element census, so "did this feature actually mesh?"
        // is measured, not inferred from the exception.
        int[] domElem = new int[nDom + 1];
        for (String ty : VOL_TYPES) {
            try { int[] ent = (int[]) invoke(ms, "getElemEntity", ty);
                  if (ent != null) for (int e : ent)
                      if (e >= 1 && e <= nDom) domElem[e]++; } catch (Throwable t) {}
        }
        List<Integer> residual = new ArrayList<>();
        for (int d = 1; d <= nDom; d++) if (domElem[d] == 0) residual.add(d);
        out.put("residual_domains", residual);
        out.put("residual_count", residual.size());
        out.put("meshed_count", nDom - residual.size());

        out.put("mesh_hasProblems", tryB(ms, "hasProblems"));
        out.put("mesh_problems", strArr(ms, "problems"));
        out.put("mesh_problemNames", strArr(ms, "problemNames"));

        List<Map<String,Object>> rows = new ArrayList<>();
        String[] tags = new String[0];
        try { tags = ms.feature().tags(); } catch (Throwable t) { notes.add("tags: " + t); }
        for (String tg : tags) {
            Map<String,Object> r = new LinkedHashMap<>();
            r.put("tag", tg);
            try { r.put("type", String.valueOf(invoke(ms.feature(tg), "getType"))); } catch (Throwable t) {}
            try { r.put("isActive", invoke(ms.feature(tg), "isActive")); } catch (Throwable t) {}
            int[] ents = null;
            try { ents = (int[]) invoke(ms.feature(tg).selection(), "entities"); } catch (Throwable t) {}
            if (ents != null) {
                r.put("n_selected", ents.length);
                if (ents.length <= 40) { List<Integer> l = new ArrayList<>();
                    for (int e : ents) l.add(e); r.put("selected", l); }
                int meshed = 0;
                for (int e : ents) if (e >= 1 && e <= nDom && domElem[e] > 0) meshed++;
                r.put("selected_meshed", meshed);
                r.put("selected_unmeshed", ents.length - meshed);
            }
            try { r.put("isRemaining", invoke(ms.feature(tg).selection(), "isRemaining")); } catch (Throwable t) {}
            // THE PAYLOAD: the per-feature build record.
            Map<String,Object> build = new LinkedHashMap<>();
            for (String p : BUILD_PROPS) {
                try {
                    Object v = invokeS(ms.feature(tg), "getString", p);
                    String s = v == null ? null : String.valueOf(v);
                    if (s != null && !s.isEmpty())
                        build.put(p, s.length() > 4000
                                ? s.substring(0, 4000) + "…[trunc]" : s);
                } catch (Throwable t) { /* property absent on this feature */ }
            }
            r.put("build_record", build);
            try { r.put("hasProblems", invoke(ms.feature(tg), "hasProblems")); } catch (Throwable t) {}
            try { r.put("problemNames", strArr(ms.feature(tg), "problemNames")); } catch (Throwable t) {}
            try { r.put("problems", strArr(ms.feature(tg), "problems")); } catch (Throwable t) {}
            rows.add(r);
        }
        out.put("features", rows);
        out.put("feature_count", rows.size());
        return out;
    }

    static Object tryB(Object o, String mth) { try { return invoke(o, mth); } catch (Throwable t) { return null; } }
    static List<String> strArr(Object o, String mth) {
        try { Object v = invoke(o, mth);
              if (v instanceof String[]) return Arrays.asList((String[]) v); } catch (Throwable t) {}
        return null;
    }
    static Object invokeS(Object o, String method, String arg) throws Exception {
        return o.getClass().getMethod(method, String.class).invoke(o, arg);
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
        throw new NoSuchMethodException(method);
    }
}
