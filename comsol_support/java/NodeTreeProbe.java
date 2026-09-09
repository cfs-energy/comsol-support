/**
 * NodeTreeProbe — load a .mph and walk its Model object tree looking
 * for any node that exposes warning/problem/message/info state.
 *
 * Usage:
 *   java NodeTreeProbe <path.mph> <raw_output_path>
 *
 * Exits 0 on success.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.io.PrintStream;
import java.io.FileOutputStream;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.regex.Pattern;

public class NodeTreeProbe {

    static PrintStream RAW;
    static final Pattern KW =
        Pattern.compile("(?i)warning|message|info|log|problem|feedback|issue|notif|diagnos|alert|consistent");

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: NodeTreeProbe <path.mph> <raw_output>");
            System.exit(1);
        }
        String mphPath = args[0];
        String rawPath = args[1];
        RAW = new PrintStream(new FileOutputStream(rawPath), true);

        ModelUtil.initStandalone(false);
        Model model = ModelUtil.load("probe_model", mphPath);

        section("groupByType() + Model.toString()");
        try {
            // groupByType() is a boolean getter on AbstractModel (paired with setGroupByType(boolean))
            Object gbt = model.groupByType();
            RAW.println("model.groupByType() = " + gbt);
        } catch (Throwable t) {
            RAW.println("groupByType threw: " + t);
        }
        try {
            RAW.println("model.toString() = " + model.toString());
        } catch (Throwable t) {
            RAW.println("toString threw: " + t);
        }
        // Also try nodeGroup() as that dumps the tree
        try {
            Object ng = model.nodeGroup();
            RAW.println("model.nodeGroup()class=" + (ng==null?"null":ng.getClass().getName()));
            if (ng != null) {
                try {
                    Method mTags = ng.getClass().getMethod("tags");
                    Object t = mTags.invoke(ng);
                    if (t instanceof String[]) RAW.println("  nodeGroup().tags=" + Arrays.toString((String[])t));
                } catch (NoSuchMethodException ignore) {}
            }
        } catch (Throwable t) {
            RAW.println("nodeGroup threw: " + t);
        }

        // List-style accessors from the task spec
        String[] lists = {
            "component","sol","study","result","view","mesh","material",
            "func","param","variable","geom","common","batch","probe",
            "pair","multiphysics","coordSystem","cpl","extraDim","frame",
            "massProp","selection"
        };

        for (String acc : lists) {
            section("Accessor: " + acc + "()");
            try {
                Method mList = model.getClass().getMethod(acc);
                Object listObj = mList.invoke(model);
                RAW.println("  listClass=" + safeClass(listObj));
                if (listObj == null) continue;

                // Try tags()
                String[] tags = null;
                try {
                    Method mTags = listObj.getClass().getMethod("tags");
                    Object to = mTags.invoke(listObj);
                    if (to instanceof String[]) tags = (String[]) to;
                } catch (NoSuchMethodException ignore) {
                    RAW.println("  [no tags() on list]");
                }

                if (tags != null) {
                    RAW.println("  tags=" + Arrays.toString(tags));
                    // Per-tag getter (same accessor name, String arg)
                    Method mGet = null;
                    try {
                        mGet = model.getClass().getMethod(acc, String.class);
                    } catch (NoSuchMethodException e) {
                        RAW.println("  [no per-tag getter " + acc + "(String)]");
                    }
                    if (mGet != null) {
                        for (String tag : tags) {
                            try {
                                Object node = mGet.invoke(model, tag);
                                inspectNode(acc, tag, node, 0);
                            } catch (Throwable t) {
                                RAW.println("    tag=" + tag + " getter threw: " + t);
                            }
                        }
                    }
                }
            } catch (NoSuchMethodException e) {
                RAW.println("  [no Model." + acc + "() method]");
            } catch (Throwable t) {
                RAW.println("  accessor threw: " + t);
            }
        }

        // Special: dive into solver/mesh/geom for their inner problem-nodes
        section("Deep probe: SolverSequence inner problems");
        try {
            String[] solTags = model.sol().tags();
            for (String tag : solTags) {
                SolverSequence ss = model.sol(tag);
                RAW.println("  sol " + tag + " hasProblems=" + tryBool(ss,"hasProblems"));
                RAW.println("    getErrorMessage=" + safeStr(() -> ss.getErrorMessage()));
                RAW.println("    getWarningMessage=" + safeStr(() -> ss.getWarningMessage()));
                RAW.println("    getInformationMessage=" + safeStr(() -> ss.getInformationMessage()));
                // SolverSequence.feature() returns a list; problemNames on the list
                try {
                    Object flist = ss.feature();
                    RAW.println("    feature()class=" + safeClass(flist));
                    // try problemNames via reflection
                    tryCallStringArray(flist, "problemNames", "sol " + tag + ".feature().problemNames");
                    // walk each feature for problem/warning/info state
                    String[] ftags = (String[]) flist.getClass().getMethod("tags").invoke(flist);
                    for (String ft : ftags) {
                        Object sf = ss.feature(ft);
                        probeProblemy("sol " + tag + ".feature(" + ft + ")", sf);
                    }
                } catch (Throwable t) {
                    RAW.println("    feature() dive threw: " + t);
                }
            }
        } catch (Throwable t) {
            RAW.println("  sol dive threw: " + t);
        }

        section("Deep probe: MeshSequence inner problems");
        try {
            String[] mtags = model.mesh().tags();
            for (String tag : mtags) {
                MeshSequence ms = model.mesh(tag);
                RAW.println("  mesh " + tag + " hasProblems=" + tryBool(ms,"hasProblems"));
                try {
                    Object flist = ms.feature();
                    RAW.println("    feature()class=" + safeClass(flist));
                    tryCallStringArray(flist, "problemNames", "mesh " + tag + ".feature().problemNames");
                    String[] ftags = (String[]) flist.getClass().getMethod("tags").invoke(flist);
                    for (String ft : ftags) {
                        Object mf = ms.feature(ft);
                        probeProblemy("mesh " + tag + ".feature(" + ft + ")", mf);
                    }
                } catch (Throwable t) {
                    RAW.println("    feature() dive threw: " + t);
                }
            }
        } catch (Throwable t) {
            RAW.println("  mesh dive threw: " + t);
        }

        section("Deep probe: Component physics + materials");
        try {
            String[] ctags = model.component().tags();
            for (String ctag : ctags) {
                Object comp = model.component(ctag);
                RAW.println("  component(" + ctag + ") class=" + safeClass(comp));
                // try physics() accessor
                for (String sub : new String[]{"physics","material","multiphysics","cpl","coordSystem","pair","common","selection","variable","probe","mesh"}) {
                    try {
                        Method mL = comp.getClass().getMethod(sub);
                        Object lo = mL.invoke(comp);
                        if (lo == null) continue;
                        String[] tags = null;
                        try {
                            tags = (String[]) lo.getClass().getMethod("tags").invoke(lo);
                        } catch (NoSuchMethodException ignore) {}
                        if (tags == null || tags.length == 0) continue;
                        RAW.println("    comp." + sub + " tags=" + Arrays.toString(tags));
                        Method mGet = comp.getClass().getMethod(sub, String.class);
                        for (String t : tags) {
                            Object pn = mGet.invoke(comp, t);
                            RAW.println("      " + sub + "(" + t + ") class=" + safeClass(pn));
                            probeProblemy("comp." + sub + "(" + t + ")", pn);
                            // Feature dive for physics and multiphysics
                            try {
                                Method fM = pn.getClass().getMethod("feature");
                                Object flist = fM.invoke(pn);
                                String[] ftags = (String[]) flist.getClass().getMethod("tags").invoke(flist);
                                if (ftags.length > 0) {
                                    RAW.println("        feature.tags=" + Arrays.toString(ftags));
                                    Method fGet = pn.getClass().getMethod("feature", String.class);
                                    for (String ft : ftags) {
                                        Object pf = fGet.invoke(pn, ft);
                                        probeProblemy("comp." + sub + "(" + t + ").feature(" + ft + ")", pf);
                                    }
                                }
                            } catch (NoSuchMethodException ignore) {
                            } catch (Throwable t3) {
                                RAW.println("        feature dive threw: " + t3);
                            }
                        }
                    } catch (NoSuchMethodException ignore) {
                    } catch (Throwable t2) {
                        RAW.println("    comp." + sub + " threw: " + t2);
                    }
                }
            }
        } catch (Throwable t) {
            RAW.println("  component dive threw: " + t);
        }

        section("Deep probe: GeomSequence inner problems");
        try {
            String[] gtags = model.geom().tags();
            for (String tag : gtags) {
                GeomSequence gs = model.geom(tag);
                try {
                    Object flist = gs.feature();
                    RAW.println("  geom " + tag + " feature()class=" + safeClass(flist));
                    String[] ftags = (String[]) flist.getClass().getMethod("tags").invoke(flist);
                    for (String ft : ftags) {
                        Object gf = gs.feature(ft);
                        probeProblemy("geom " + tag + ".feature(" + ft + ")", gf);
                    }
                } catch (Throwable t) {
                    RAW.println("    feature() dive threw: " + t);
                }
            }
        } catch (Throwable t) {
            RAW.println("  geom dive threw: " + t);
        }

        // Enumerate every reachable class name we've seen in the dive
        section("Distinct class names reached from Model (depth 2 via list+feature)");
        java.util.TreeSet<String> classNames = new java.util.TreeSet<>();
        for (String acc : lists) {
            try {
                Method mList = model.getClass().getMethod(acc);
                Object listObj = mList.invoke(model);
                if (listObj == null) continue;
                classNames.add(listObj.getClass().getName());
                try {
                    String[] tags = (String[]) listObj.getClass().getMethod("tags").invoke(listObj);
                    Method mGet = model.getClass().getMethod(acc, String.class);
                    for (String t : tags) {
                        Object n = mGet.invoke(model, t);
                        if (n != null) classNames.add(n.getClass().getName());
                    }
                } catch (Throwable ignore) {}
            } catch (Throwable ignore) {}
        }
        for (String n : classNames) RAW.println("  " + n);

        RAW.flush();
        RAW.close();
        System.out.println("{\"success\":true,\"raw\":\"" + rawPath + "\"}");
        System.exit(0);
    }

    // ---- Helpers ----

    static void section(String title) {
        RAW.println();
        RAW.println("====================================================");
        RAW.println("== " + title);
        RAW.println("====================================================");
    }

    static void inspectNode(String acc, String tag, Object node, int depth) {
        if (node == null) {
            RAW.println("    " + acc + "(" + tag + ") = null");
            return;
        }
        String cn = node.getClass().getName();
        RAW.println("    " + acc + "(" + tag + ") class=" + cn);
        // List matching methods
        java.util.List<String> matching = new java.util.ArrayList<>();
        for (Method mm : node.getClass().getMethods()) {
            if (KW.matcher(mm.getName()).find()) {
                matching.add(mm.getName() + "/" + mm.getParameterCount());
            }
        }
        if (!matching.isEmpty()) {
            RAW.println("      kw-methods: " + matching);
            probeProblemy("    " + acc + "(" + tag + ")", node);
        }
    }

    /** For a node of any type, attempt to call known diagnostic accessors
     *  via reflection and report non-empty state. */
    static void probeProblemy(String label, Object node) {
        if (node == null) return;
        Class<?> cls = node.getClass();

        // hasProblems / hasProblem / hasWarning / hasInformation
        for (String mname : new String[]{"hasProblems","hasProblem","hasWarning","hasInformation","hasProblemsOrInformation","hasProblemOrInformation"}) {
            try {
                Method m = cls.getMethod(mname);
                Object v = m.invoke(node);
                if (Boolean.TRUE.equals(v)) {
                    RAW.println("      " + label + "." + mname + "() = TRUE");
                } else if (v instanceof Boolean) {
                    RAW.println("      " + label + "." + mname + "() = false");
                }
            } catch (NoSuchMethodException ignore) {
            } catch (Throwable t) {
                RAW.println("      " + label + "." + mname + "() threw: " + t);
            }
        }

        // getErrorMessage / getWarningMessage / getInformationMessage
        for (String mname : new String[]{"getErrorMessage","getWarningMessage","getInformationMessage"}) {
            try {
                Method m = cls.getMethod(mname);
                Object v = m.invoke(node);
                if (v instanceof String && !((String)v).isEmpty()) {
                    RAW.println("      " + label + "." + mname + "() = \"" + v + "\"");
                }
            } catch (NoSuchMethodException ignore) {
            } catch (Throwable t) {
                RAW.println("      " + label + "." + mname + "() threw: " + t);
            }
        }

        // problems() / warnings() -> these may return sub-lists OR String[]
        for (String mname : new String[]{"problems","warnings","message","buildInfo","info","infoCurrent"}) {
            try {
                Method m = cls.getMethod(mname);
                Object v = m.invoke(node);
                RAW.println("      " + label + "." + mname + "() -> " + safeClass(v));
                // If it's a String[] or String, report value directly
                if (v instanceof String[]) {
                    String[] arr = (String[]) v;
                    RAW.println("        value[String[]] len=" + arr.length + " " + Arrays.toString(arr));
                } else if (v instanceof String) {
                    String s = (String) v;
                    if (!s.isEmpty()) RAW.println("        value[String]=\"" + s + "\"");
                }
                // If it's a *ProblemFeatureList, call warningNames/errorNames/informationNames
                if (v != null && !(v instanceof String[]) && !(v instanceof String)) {
                    for (String inner : new String[]{"warningNames","errorNames","informationNames"}) {
                        try {
                            Method im = v.getClass().getMethod(inner);
                            Object iv = im.invoke(v);
                            if (iv instanceof String[]) {
                                String[] arr = (String[]) iv;
                                RAW.println("        ." + inner + "() = [len=" + arr.length + "] " + Arrays.toString(arr));
                            }
                        } catch (NoSuchMethodException ignore) {}
                    }
                    // also attempt tags()
                    try {
                        Method tm = v.getClass().getMethod("tags");
                        Object to = tm.invoke(v);
                        if (to instanceof String[]) {
                            RAW.println("        .tags() = " + Arrays.toString((String[])to));
                        }
                    } catch (NoSuchMethodException ignore) {}
                }
            } catch (NoSuchMethodException ignore) {
            } catch (Throwable t) {
                RAW.println("      " + label + "." + mname + "() threw: " + t);
            }
        }
    }

    static void tryCallStringArray(Object target, String method, String label) {
        try {
            Method m = target.getClass().getMethod(method);
            Object v = m.invoke(target);
            if (v instanceof String[]) {
                String[] arr = (String[]) v;
                RAW.println("    " + label + " = [len=" + arr.length + "] " + Arrays.toString(arr));
            }
        } catch (NoSuchMethodException ignore) {
            RAW.println("    [" + label + " no such method]");
        } catch (Throwable t) {
            RAW.println("    " + label + " threw: " + t);
        }
    }

    static Boolean tryBool(Object target, String method) {
        try {
            Method m = target.getClass().getMethod(method);
            Object v = m.invoke(target);
            return (v instanceof Boolean) ? (Boolean) v : null;
        } catch (Throwable t) {
            return null;
        }
    }

    static String safeClass(Object o) {
        return o == null ? "null" : o.getClass().getName();
    }

    interface StrSupplier { String get() throws Exception; }

    static String safeStr(StrSupplier s) {
        try {
            String v = s.get();
            return v == null ? "null" : ("\"" + v + "\"");
        } catch (Throwable t) {
            return "threw " + t.getClass().getSimpleName();
        }
    }
}
