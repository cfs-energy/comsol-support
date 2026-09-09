/**
 * SolverTelemetry — stdout-marshaled event emitter for the comsol-support
 * telemetry loop.
 *
 * Every event is one line on stdout prefixed with "TELEMETRY: " followed
 * by a single JSON object. The Python side (mphgen.py) picks these lines
 * out of the subprocess stdout without interfering with the existing
 * final JSON envelope.
 *
 * Event schema (stable, model- and study-type-agnostic):
 *   {
 *     "event_type": "run_start" | "build_done" | "solve_start" |
 *                   "solve_done" | "save_done" | "partial_save" |
 *                   "partial_save_failed" | "halt" | string,
 *     "wall_ms":    long,        // wall time since emitter construction
 *     "payload":    { ... }      // event-specific fields (may be {})
 *   }
 *
 * Stdout marshaling is the chosen transport because the COMSOL sandbox
 * blocks PrintWriter writes from builder Java to workspace paths; stdout
 * is reliably plumbed through to the Python subprocess consumer.
 *
 * Not thread-safe. Single-threaded emitter, which matches the
 * ModelExporter call flow.
 *
 * The wall-clock origin is a static field. If a caller reuses the same
 * JVM for multiple runs, invoke reset() at the start of each run so
 * wall_ms values are zeroed against the run's start rather than the
 * JVM's class-load time.
 */

public class SolverTelemetry {

    public static final String PREFIX = "TELEMETRY: ";

    private static long t0 = System.currentTimeMillis();

    /** Reset the wall-clock origin. Call once at harness start. */
    public static void reset() {
        t0 = System.currentTimeMillis();
    }

    /** Emit an event with no payload. */
    public static void emit(String eventType) {
        emit(eventType, null);
    }

    /** Emit an event with a pre-encoded JSON-object payload (may be null). */
    public static void emit(String eventType, String payloadJson) {
        long wallMs = System.currentTimeMillis() - t0;
        String payload = (payloadJson == null || payloadJson.isEmpty())
                ? "{}" : payloadJson;
        StringBuilder sb = new StringBuilder(128);
        sb.append(PREFIX);
        sb.append("{\"event_type\":\"").append(escapeJson(eventType)).append("\",");
        sb.append("\"wall_ms\":").append(wallMs).append(",");
        sb.append("\"payload\":").append(payload);
        sb.append("}");
        System.out.println(sb.toString());
        System.out.flush();
    }

    /**
     * Convenience: emit an event carrying a single string field in its
     * payload. Avoids ad-hoc JSON concatenation at call sites.
     */
    public static void emitString(String eventType, String key, String value) {
        String payload = "{\"" + escapeJson(key) + "\":\""
                + escapeJson(value == null ? "" : value) + "\"}";
        emit(eventType, payload);
    }

    /**
     * Emit an error event carrying the full exception-chain detail.
     *
     * Payload: {"message": "<class>: <msg>", "error_detail": ["..."]}
     *
     * This is the canonical way for a mutator that SWALLOWS an exception
     * (e.g. a mesh build that throws but has committed a usable partial
     * mesh — see docs/meshing.md §7) to keep the failure visible: the
     * harness envelope only carries error_detail for exceptions that
     * reach it, so a swallowed exception is otherwise invisible
     * (halt_reason=success, empty error_detail). Events named
     * "&lt;anything&gt;_error" are counted and their detail accumulated by the
     * Python telemetry digest (error_events / error_event_detail).
     *
     * Convention: use an event type ending in "_error" (e.g.
     * "mesh_error") so the digest classifies it as an error event.
     */
    public static void emitError(String eventType, Throwable t) {
        java.util.List<String> lines = errorDetail(t);
        StringBuilder arr = new StringBuilder("[");
        for (int i = 0; i < lines.size(); i++) {
            if (i > 0) arr.append(",");
            arr.append("\"").append(escapeJson(lines.get(i))).append("\"");
        }
        arr.append("]");
        String msg = (t == null) ? ""
                : t.getClass().getName()
                  + (t.getMessage() == null ? "" : ": " + t.getMessage());
        emit(eventType, payloadMixed(
                "message", msg,
                "error_detail", new Raw(arr.toString())));
    }

    /**
     * Best-effort resident-set-size sampling for heartbeat / last-gasp
     * payloads. Reads Linux {@code /proc/self/status} VmRSS; returns -1
     * when unavailable (non-Linux, read denied). Never throws. Added
     * A meshing campaign once lost a whole strategy branch to an
     * OOM narrative that no artifact could confirm or refute because
     * heartbeats carried no memory signal.
     */
    public static long processRssBytes() {
        try {
            for (String s : java.nio.file.Files.readAllLines(
                    java.nio.file.Paths.get("/proc/self/status"))) {
                if (s.startsWith("VmRSS:")) {
                    String[] parts = s.substring("VmRSS:".length())
                            .replace("kB", "").trim().split("\\s+");
                    return Long.parseLong(parts[0]) * 1024L;
                }
            }
        } catch (Throwable ignored) {
            // Non-Linux or unreadable — sampling is best-effort.
        }
        return -1L;
    }

    /**
     * Harvest COMSOL's detailed diagnostic lines from an exception
     * chain. COMSOL's FlException carries the useful diagnostics in
     * getTranslatableMessageArray() / getMessages(), not getMessage() —
     * accessed reflectively so this class keeps its no-COMSOL-classpath
     * property (compiles and runs anywhere). Returns a de-duplicated,
     * order-preserving list of non-empty lines with each link's generic
     * getMessage() appended as fallback. Best-effort: every reflective
     * step is swallowed. Canonical implementation — ModelExporter
     * delegates here.
     */
    public static java.util.List<String> errorDetail(Throwable t) {
        java.util.LinkedHashSet<String> lines = new java.util.LinkedHashSet<>();
        java.util.Set<Throwable> seen =
                java.util.Collections.newSetFromMap(
                        new java.util.IdentityHashMap<Throwable, Boolean>());
        Throwable cur = t;
        while (cur != null && seen.add(cur)) {
            for (String name : new String[] {
                    "getTranslatableMessageArray", "getMessages" }) {
                try {
                    java.lang.reflect.Method m = cur.getClass().getMethod(name);
                    Object r = m.invoke(cur);
                    if (r instanceof String[]) {
                        for (String s : (String[]) r) {
                            if (s != null && !s.trim().isEmpty()) {
                                lines.add(s.trim());
                            }
                        }
                    }
                } catch (Throwable ignored) {
                    // Method absent / inaccessible on this type or COMSOL
                    // version — skip and keep best-effort harvesting.
                }
            }
            String gm = cur.getMessage();
            if (gm != null && !gm.trim().isEmpty()) lines.add(gm.trim());
            cur = cur.getCause();
        }
        return new java.util.ArrayList<>(lines);
    }

    /**
     * Build a minimal payload JSON object from key/value pairs. Values
     * are emitted as strings (quoted). For richer payloads, compose
     * the JSON at the call site and pass via emit(String, String).
     */
    public static String payload(String... kv) {
        if (kv == null || kv.length == 0) return "{}";
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(escapeJson(kv[i])).append("\":");
            sb.append("\"").append(escapeJson(kv[i + 1] == null ? "" : kv[i + 1]))
              .append("\"");
        }
        sb.append("}");
        return sb.toString();
    }

    /**
     * Build a payload JSON object with mixed-type values. Numbers and
     * booleans are emitted as JSON primitives (not quoted); null is
     * emitted as the JSON null literal; everything else is stringified
     * and quoted. Non-finite Double/Float values (NaN / +-Infinity) are
     * emitted as quoted strings since JSON has no representation for
     * them — consumers must parse accordingly.
     *
     * Intended for future per-study adapters that want to emit numeric
     * step data (dt, order, iteration counts) alongside string fields.
     */
    public static String payloadMixed(Object... kv) {
        if (kv == null || kv.length == 0) return "{}";
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if (i > 0) sb.append(",");
            Object key = kv[i];
            sb.append("\"")
              .append(escapeJson(key == null ? "" : key.toString()))
              .append("\":");
            sb.append(jsonValue(kv[i + 1]));
        }
        sb.append("}");
        return sb.toString();
    }

    /**
     * Wrapper marking a pre-encoded JSON fragment to embed verbatim in a
     * {@link #payloadMixed} value position (e.g. a JSON array or nested
     * object the caller has already rendered). The caller is responsible
     * for the fragment being valid JSON; a null/empty fragment becomes
     * the JSON null literal. Topic-agnostic — just a transport detail.
     */
    public static final class Raw {
        final String json;
        public Raw(String json) {
            this.json = (json == null || json.isEmpty()) ? "null" : json;
        }
    }

    /** Render an Object as a JSON value literal. */
    private static String jsonValue(Object v) {
        if (v == null) return "null";
        if (v instanceof Raw) return ((Raw) v).json;
        if (v instanceof Boolean) return v.toString();
        if (v instanceof Double) {
            double d = (Double) v;
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                return "\"" + escapeJson(Double.toString(d)) + "\"";
            }
            return Double.toString(d);
        }
        if (v instanceof Float) {
            float f = (Float) v;
            if (Float.isNaN(f) || Float.isInfinite(f)) {
                return "\"" + escapeJson(Float.toString(f)) + "\"";
            }
            return Float.toString(f);
        }
        if (v instanceof Number) return v.toString();
        return "\"" + escapeJson(v.toString()) + "\"";
    }

    /** JSON string escape (mirrors ModelExporter.escapeJson). */
    public static String escapeJson(String s) {
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
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }
}
