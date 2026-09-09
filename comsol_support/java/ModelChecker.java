/**
 * ModelChecker — Layer B of the linting pipeline.
 *
 * Loads an .mph file, walks parameters and component variables, and
 * writes two sidecar JSONs next to the .mph (or to the paths given
 * via --units-out / --descr-out):
 *
 *   <mph>.units.json         — layer_b.errors per-parameter
 *   <mph>.descriptions.json  — layer_b.missing_runtime, placeholder
 *
 * SCOPE (narrower than the original plan — see comsol_linting_research.md):
 *   - Parameter unit anomalies (via ParamBase.evaluateUnit).
 *   - Missing or placeholder descriptions on params and variables.
 *   - GUI yellow warnings for PDE-slot semantic mismatches are NOT
 *     accessible via the Java API and are not reported.
 *
 * Usage:
 *   java ModelChecker --mph <path.mph>
 *                     [--units-out <path>]
 *                     [--descr-out <path>]
 *
 * Prints one JSON envelope on stdout: {"success":true,
 *   "units_out":"...","descr_out":"...","n_params":N,"n_variables":N,
 *   "n_unit_errors":N,"n_missing":N,"n_placeholder":N,"elapsed_ms":N}
 * On failure, prints {"success":false,"error":"..."}.
 * Exits 0 on success, 1 on error.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

public class ModelChecker {

    // Placeholder descriptions — same heuristic as the Python side.
    private static final Pattern PLACEHOLDER = Pattern.compile(
        "^\\s*(todo|tbd|xxx|pending|fixme|\\.\\.\\.|\\?+|n/a)\\s*$",
        Pattern.CASE_INSENSITIVE);

    public static void main(String[] argv) {
        String mphPath = null;
        String unitsOut = null;
        String descrOut = null;
        String symbolsOut = null;
        String slotsOut = null;
        String slotsVersion = null;

        for (int i = 0; i < argv.length; i++) {
            String a = argv[i];
            if ("--mph".equals(a) && i + 1 < argv.length) {
                mphPath = argv[++i];
            } else if ("--units-out".equals(a) && i + 1 < argv.length) {
                unitsOut = argv[++i];
            } else if ("--descr-out".equals(a) && i + 1 < argv.length) {
                descrOut = argv[++i];
            } else if ("--symbols-out".equals(a) && i + 1 < argv.length) {
                symbolsOut = argv[++i];
            } else if ("--slots-out".equals(a) && i + 1 < argv.length) {
                slotsOut = argv[++i];
            } else if ("--slots-version".equals(a) && i + 1 < argv.length) {
                slotsVersion = argv[++i];
            } else {
                emitError("Unknown argument: " + a);
                System.exit(1);
            }
        }

        if (mphPath == null) {
            emitError("Required: --mph <path.mph>");
            System.exit(1);
        }
        if (unitsOut == null) unitsOut = mphPath + ".units.json";
        if (descrOut == null) descrOut = mphPath + ".descriptions.json";
        if (symbolsOut == null) symbolsOut = mphPath + ".symbols.json";

        long t0 = System.currentTimeMillis();
        try {
            ModelUtil.initStandalone(false);

            // Unique tag per load so successive runs in one JVM don't clash.
            String tag = "lint_" + System.currentTimeMillis();
            Model model = ModelUtil.load(tag, mphPath);

            String modelSha = sha256(Paths.get(mphPath));

            Walked w = walk(model);

            writeUnitsJson(unitsOut, w.unitErrors, modelSha, mphPath);
            writeDescrJson(descrOut, w.missing, w.placeholder, modelSha, mphPath);
            writeSymbolsJson(symbolsOut, w.params, w.variables,
                             modelSha, mphPath);

            // Optional: emit slot records in the same JVM session.
            // This avoids a second COMSOL load when cmd_check --db is
            // used (model-load is ~30s on typical models).
            int nSlots = 0;
            int nVarUnits = 0;
            if (slotsOut != null) {
                String cv = slotsVersion == null ? "unknown" : slotsVersion;
                Path sp = Paths.get(slotsOut);
                if (sp.getParent() != null) {
                    Files.createDirectories(sp.getParent());
                }
                try (java.io.BufferedWriter sw = Files.newBufferedWriter(
                        sp, StandardCharsets.UTF_8,
                        java.nio.file.StandardOpenOption.CREATE,
                        java.nio.file.StandardOpenOption.TRUNCATE_EXISTING)) {
                    SlotHarvester.HarvestResult hr =
                        SlotHarvester.harvestLoadedModel(model, mphPath, cv, sw);
                    nSlots = hr.slots;
                    nVarUnits = hr.varUnits;
                }
            }

            long elapsed = System.currentTimeMillis() - t0;

            emit("{\"success\":true,"
                + "\"units_out\":\"" + escape(unitsOut) + "\","
                + "\"descr_out\":\"" + escape(descrOut) + "\","
                + "\"symbols_out\":\"" + escape(symbolsOut) + "\","
                + (slotsOut != null
                    ? "\"slots_out\":\"" + escape(slotsOut) + "\","
                    : "")
                + "\"n_params\":" + w.nParams + ","
                + "\"n_variables\":" + w.nVariables + ","
                + "\"n_unit_errors\":" + w.unitErrors.size() + ","
                + "\"n_missing\":" + w.missing.size() + ","
                + "\"n_placeholder\":" + w.placeholder.size() + ","
                + "\"n_slots\":" + nSlots + ","
                + "\"n_var_units\":" + nVarUnits + ","
                + "\"elapsed_ms\":" + elapsed + "}");

            // Same exit pattern as ModelExporter — skip disconnect().
            System.exit(0);
        } catch (Exception e) {
            emitException(e);
            System.exit(1);
        }
    }

    // ---- Walk ----

    static class UnitError {
        String node;   // e.g. "param/rho_air_val"
        String name;
        String expression;
        String error;
    }

    static class DescrEntry {
        String node;       // "param" or "comp1/var_b"
        String kind;       // "param" | "variable"
        String name;
        String descr;      // empty if missing
    }

    static class Symbol {
        String kind;        // "param" | "variable"
        String scope;       // "param" or "comp/varTag"
        String name;
        String expression;  // raw COMSOL expression
        String unit;        // deduced unit (params only, from evaluateUnit)
    }

    static class Walked {
        int nParams;
        int nVariables;
        List<UnitError> unitErrors = new ArrayList<>();
        List<DescrEntry> missing = new ArrayList<>();
        List<DescrEntry> placeholder = new ArrayList<>();
        List<Symbol> params = new ArrayList<>();
        List<Symbol> variables = new ArrayList<>();
    }

    private static Walked walk(Model model) {
        Walked w = new Walked();

        // Global parameters
        String[] pnames = model.param().varnames();
        w.nParams = pnames.length;
        for (String n : pnames) {
            String expr = safeGet(() -> model.param().get(n));
            String descr = safeGet(() -> model.param().descr(n));
            classifyDescr(w, "param", "param", n, descr);

            Symbol sym = new Symbol();
            sym.kind = "param";
            sym.scope = "param";
            sym.name = n;
            sym.expression = expr == null ? "" : expr;

            try {
                String unit = model.param().evaluateUnit(n);
                sym.unit = unit == null ? "" : unit;
                // evaluateUnit typically returns the deduced unit string.
                // If COMSOL cannot deduce a consistent unit it may return
                // an empty string or a non-unit marker. Treat empty as a
                // warning only when the expression itself uses units.
                if ((unit == null || unit.isEmpty()) && expr != null
                        && expr.contains("[")) {
                    UnitError ue = new UnitError();
                    ue.node = "param";
                    ue.name = n;
                    ue.expression = expr;
                    ue.error = "evaluateUnit returned empty for a "
                             + "unit-bearing expression";
                    w.unitErrors.add(ue);
                }
            } catch (Exception ex) {
                UnitError ue = new UnitError();
                ue.node = "param";
                ue.name = n;
                ue.expression = expr;
                ue.error = ex.getMessage() != null ? ex.getMessage()
                    : ex.getClass().getSimpleName();
                w.unitErrors.add(ue);
                sym.unit = "";
            }
            w.params.add(sym);
        }

        // Component variables
        try {
            String[] compTags = model.modelNode().tags();
            for (String c : compTags) {
                String[] varTags;
                try {
                    varTags = model.modelNode(c).variable().tags();
                } catch (Exception ignore) {
                    continue;  // not all ModelNodes carry variables
                }
                for (String vTag : varTags) {
                    Expr v;
                    try {
                        v = model.variable(vTag);
                    } catch (Exception ignore) {
                        continue;
                    }
                    String[] names = safeGetArr(() -> v.varnames());
                    for (String n : names) {
                        w.nVariables++;
                        String descr = safeGet(() -> v.descr(n));
                        classifyDescr(w, c + "/" + vTag, "variable", n, descr);
                        Symbol sym = new Symbol();
                        sym.kind = "variable";
                        sym.scope = c + "/" + vTag;
                        sym.name = n;
                        sym.expression = safeGet(() -> v.get(n));
                        sym.unit = "";  // no evaluateUnit on variables
                        w.variables.add(sym);
                    }
                }
            }
        } catch (Exception ignore) {
            // No components — nothing to walk. Leave counters at zero.
        }

        return w;
    }

    private static void classifyDescr(
            Walked w, String node, String kind, String name, String descr) {
        DescrEntry e = new DescrEntry();
        e.node = node;
        e.kind = kind;
        e.name = name;
        e.descr = descr == null ? "" : descr;
        if (descr == null || descr.isEmpty()) {
            w.missing.add(e);
        } else if (PLACEHOLDER.matcher(descr).matches()
                   || descr.trim().length() < 3) {
            w.placeholder.add(e);
        }
    }

    // ---- JSON writers ----

    private static void writeUnitsJson(
            String path, List<UnitError> errors,
            String modelSha, String mphPath) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\n");
        sb.append("  \"layer_b\": {\n");
        sb.append("    \"errors\": [");
        for (int i = 0; i < errors.size(); i++) {
            UnitError e = errors.get(i);
            if (i > 0) sb.append(",");
            sb.append("\n      {");
            sb.append("\"node\":\"").append(escape(e.node)).append("\",");
            sb.append("\"name\":\"").append(escape(e.name)).append("\",");
            sb.append("\"expression\":\"").append(escape(e.expression)).append("\",");
            sb.append("\"error\":\"").append(escape(e.error)).append("\"");
            sb.append("}");
        }
        if (!errors.isEmpty()) sb.append("\n    ");
        sb.append("]\n");
        sb.append("  },\n");
        sb.append("  \"model_sha256\": \"").append(escape(modelSha)).append("\",\n");
        sb.append("  \"model_path\": \"").append(escape(mphPath)).append("\",\n");
        sb.append("  \"generated_at\": \"").append(Instant.now().toString()).append("\"\n");
        sb.append("}\n");
        atomicWrite(path, sb.toString());
    }

    private static void writeDescrJson(
            String path, List<DescrEntry> missing, List<DescrEntry> placeholder,
            String modelSha, String mphPath) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\n");
        sb.append("  \"layer_b\": {\n");
        sb.append("    \"missing_runtime\": ");
        appendDescrArray(sb, missing);
        sb.append(",\n    \"placeholder\": ");
        appendDescrArray(sb, placeholder);
        sb.append("\n  },\n");
        sb.append("  \"model_sha256\": \"").append(escape(modelSha)).append("\",\n");
        sb.append("  \"model_path\": \"").append(escape(mphPath)).append("\",\n");
        sb.append("  \"generated_at\": \"").append(Instant.now().toString()).append("\"\n");
        sb.append("}\n");
        atomicWrite(path, sb.toString());
    }

    private static void writeSymbolsJson(
            String path, List<Symbol> params, List<Symbol> variables,
            String modelSha, String mphPath) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\n");
        sb.append("  \"params\": ");
        appendSymbolArray(sb, params);
        sb.append(",\n  \"variables\": ");
        appendSymbolArray(sb, variables);
        sb.append(",\n  \"model_sha256\": \"").append(escape(modelSha)).append("\",\n");
        sb.append("  \"model_path\": \"").append(escape(mphPath)).append("\",\n");
        sb.append("  \"generated_at\": \"").append(Instant.now().toString()).append("\"\n");
        sb.append("}\n");
        atomicWrite(path, sb.toString());
    }

    private static void appendSymbolArray(StringBuilder sb, List<Symbol> ss) {
        sb.append("[");
        for (int i = 0; i < ss.size(); i++) {
            Symbol s = ss.get(i);
            if (i > 0) sb.append(",");
            sb.append("\n    {");
            sb.append("\"kind\":\"").append(escape(s.kind)).append("\",");
            sb.append("\"scope\":\"").append(escape(s.scope)).append("\",");
            sb.append("\"name\":\"").append(escape(s.name)).append("\",");
            sb.append("\"expression\":\"").append(escape(s.expression)).append("\",");
            sb.append("\"unit\":\"").append(escape(s.unit == null ? "" : s.unit)).append("\"");
            sb.append("}");
        }
        if (!ss.isEmpty()) sb.append("\n  ");
        sb.append("]");
    }

    private static void appendDescrArray(StringBuilder sb, List<DescrEntry> es) {
        sb.append("[");
        for (int i = 0; i < es.size(); i++) {
            DescrEntry e = es.get(i);
            if (i > 0) sb.append(",");
            sb.append("\n      {");
            sb.append("\"node\":\"").append(escape(e.node)).append("\",");
            sb.append("\"kind\":\"").append(escape(e.kind)).append("\",");
            sb.append("\"name\":\"").append(escape(e.name)).append("\",");
            sb.append("\"descr\":\"").append(escape(e.descr)).append("\"");
            sb.append("}");
        }
        if (!es.isEmpty()) sb.append("\n    ");
        sb.append("]");
    }

    private static void atomicWrite(String path, String content) throws IOException {
        Path p = Paths.get(path);
        if (p.getParent() != null) Files.createDirectories(p.getParent());
        Path tmp = Paths.get(path + ".tmp");
        try (Writer w = new OutputStreamWriter(
                new FileOutputStream(tmp.toFile()), StandardCharsets.UTF_8)) {
            w.write(content);
        }
        Files.move(tmp, p,
            java.nio.file.StandardCopyOption.REPLACE_EXISTING,
            java.nio.file.StandardCopyOption.ATOMIC_MOVE);
    }

    // ---- Helpers ----

    private interface StrSupplier { String get() throws Exception; }
    private interface ArrSupplier { String[] get() throws Exception; }

    private static String safeGet(StrSupplier s) {
        try { return s.get(); }
        catch (Exception e) { return ""; }
    }

    private static String[] safeGetArr(ArrSupplier s) {
        try { return s.get(); }
        catch (Exception e) { return new String[0]; }
    }

    private static String sha256(Path p) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] buf = new byte[65536];
        try (java.io.InputStream in = Files.newInputStream(p)) {
            int r;
            while ((r = in.read(buf)) > 0) md.update(buf, 0, r);
        }
        byte[] digest = md.digest();
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static void emit(String json) {
        System.out.println(json);
        System.out.flush();
    }

    private static void emitError(String msg) {
        emit("{\"success\":false,\"error\":\"" + escape(msg) + "\"}");
    }

    private static void emitException(Exception e) {
        Throwable cause = (e.getCause() != null) ? e.getCause() : e;
        String msg = cause.getMessage() != null
            ? cause.getMessage()
            : cause.getClass().getName();
        emit("{\"success\":false,\"error\":\"" + escape(msg) + "\"}");
    }

    private static String escape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
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
