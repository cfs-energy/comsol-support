/**
 * SelectionAlgebra — named selection definitions with geometric predicates
 * and boolean operations for COMSOL models.
 *
 * Pure Java (no com.comsol.model.* imports). Tracks selection definitions
 * as data structures. Generated stage code reads definitions and emits
 * the corresponding COMSOL API calls.
 *
 * Primitive selections:
 *   boxsel  = Box          ballsel = Ball         cylsel  = Cylinder
 *   expsel  = Explicit (escape hatch, Geometry stage only)
 *
 * Boolean operations:
 *   uni = Union            int = Intersection     cmp = Complement
 *   dif = Difference       adj = Adjacent
 *
 * Serializes to JSON for compaction working_memory persistence.
 */

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class SelectionAlgebra {

    // ---- Inner class ----

    public static class SelectionDef {
        public final String tag;
        public final String name;
        public final String type;   // Box, Ball, Cylinder, Explicit, Union, Intersection, Complement, Difference, Adjacent
        public final Map<String, String> params;
        public final List<String> operands;  // operand names (for boolean ops)
        public final String stage;

        public SelectionDef(String tag, String name, String type,
                            Map<String, String> params,
                            List<String> operands, String stage) {
            this.tag = tag;
            this.name = name;
            this.type = type;
            this.params = params != null
                    ? Collections.unmodifiableMap(new LinkedHashMap<>(params))
                    : Collections.emptyMap();
            this.operands = operands != null
                    ? Collections.unmodifiableList(new ArrayList<>(operands))
                    : Collections.emptyList();
            this.stage = stage != null ? stage : "selections";
        }

        @Override
        public String toString() {
            return tag + "=" + name + " (" + type + ")";
        }
    }

    // ---- State ----

    private final Map<String, SelectionDef> selections;  // name -> def
    private final Map<String, SelectionDef> tagIndex;    // tag  -> def
    private final Map<String, Integer>      counters;    // prefix -> counter

    // ---- Constructor ----

    public SelectionAlgebra() {
        this.selections = new LinkedHashMap<>();
        this.tagIndex   = new LinkedHashMap<>();
        this.counters   = new HashMap<>();
    }

    // ---- Helpers ----

    private String nextTag(String prefix) {
        int c = counters.getOrDefault(prefix, 0) + 1;
        counters.put(prefix, c);
        return prefix + c;
    }

    /**
     * Reject a null, empty or already-used name. Every creator calls this
     * BEFORE {@link #nextTag}, so a rejected create does not consume a tag
     * number (same discipline as {@code TagRegistry.create}).
     */
    private void requireFreshName(String name) {
        if (name == null || name.isEmpty()) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra: name must be non-null and non-empty");
        }
        if (selections.containsKey(name)) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra: name already exists: " + name);
        }
    }

    private void register(SelectionDef def) {
        requireFreshName(def.name);
        selections.put(def.name, def);
        tagIndex.put(def.tag, def);
    }

    private void validateOperands(String... operandNames) {
        for (String name : operandNames) {
            if (!selections.containsKey(name)) {
                throw new IllegalArgumentException(
                        "SelectionAlgebra: operand not found: " + name);
            }
        }
    }

    // ---- Primitive selection creators ----

    /**
     * Create a box selection.
     * @param name   human-readable name
     * @param bounds keys: xmin, xmax, ymin, ymax, zmin, zmax, condition (inside/intersects)
     * @return generated tag, e.g., "boxsel1"
     */
    public String createBox(String name, Map<String, String> bounds) {
        requireFreshName(name);
        String tag = nextTag("boxsel");
        register(new SelectionDef(tag, name, "Box", bounds,
                Collections.emptyList(), "selections"));
        return tag;
    }

    /**
     * Create a ball selection.
     * @param params keys: x, y, z, r, condition
     * @return generated tag, e.g., "ballsel1"
     */
    public String createBall(String name, Map<String, String> params) {
        requireFreshName(name);
        String tag = nextTag("ballsel");
        register(new SelectionDef(tag, name, "Ball", params,
                Collections.emptyList(), "selections"));
        return tag;
    }

    /**
     * Create a cylinder selection.
     * @param params keys: x, y, z, r, h, axis, condition
     * @return generated tag, e.g., "cylsel1"
     */
    public String createCylinder(String name, Map<String, String> params) {
        requireFreshName(name);
        String tag = nextTag("cylsel");
        register(new SelectionDef(tag, name, "Cylinder", params,
                Collections.emptyList(), "selections"));
        return tag;
    }

    /**
     * Create an explicit entity selection (Geometry stage escape hatch).
     * @param name      human-readable name
     * @param dimension 0=point, 1=edge, 2=boundary, 3=domain
     * @param entities  entity indices
     * @return generated tag, e.g., "expsel1"
     */
    public String createExplicit(String name, int dimension, int[] entities) {
        Map<String, String> params = new LinkedHashMap<>();
        params.put("dimension", String.valueOf(dimension));
        StringBuilder entStr = new StringBuilder();
        for (int i = 0; i < entities.length; i++) {
            if (i > 0) entStr.append(",");
            entStr.append(entities[i]);
        }
        params.put("entities", entStr.toString());
        requireFreshName(name);
        String tag = nextTag("expsel");
        register(new SelectionDef(tag, name, "Explicit", params,
                Collections.emptyList(), "selections"));
        return tag;
    }

    // ---- Boolean operation creators ----

    /**
     * Create a union of named selections.
     * @param name         human-readable name
     * @param operandNames names of existing selections
     * @return generated tag, e.g., "uni1"
     */
    public String createUnion(String name, String... operandNames) {
        if (operandNames.length < 2) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra.createUnion: need at least 2 operands");
        }
        validateOperands(operandNames);
        requireFreshName(name);
        String tag = nextTag("uni");
        register(new SelectionDef(tag, name, "Union", Collections.emptyMap(),
                Arrays.asList(operandNames), "selections"));
        return tag;
    }

    /**
     * Create an intersection of named selections.
     * @param name         human-readable name
     * @param operandNames names of existing selections
     * @return generated tag, e.g., "int1"
     */
    public String createIntersection(String name, String... operandNames) {
        if (operandNames.length < 2) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra.createIntersection: need at least 2 operands");
        }
        validateOperands(operandNames);
        requireFreshName(name);
        String tag = nextTag("int");
        register(new SelectionDef(tag, name, "Intersection",
                Collections.emptyMap(),
                Arrays.asList(operandNames), "selections"));
        return tag;
    }

    /**
     * Create a complement of a named selection.
     * @param name        human-readable name
     * @param operandName name of existing selection to complement
     * @return generated tag, e.g., "cmp1"
     */
    public String createComplement(String name, String operandName) {
        validateOperands(operandName);
        requireFreshName(name);
        String tag = nextTag("cmp");
        register(new SelectionDef(tag, name, "Complement",
                Collections.emptyMap(),
                Collections.singletonList(operandName), "selections"));
        return tag;
    }

    /**
     * Create a difference selection (base minus subtract).
     * @param name         human-readable name
     * @param baseName     name of the base selection
     * @param subtractName name of the selection to subtract
     * @return generated tag, e.g., "dif1"
     */
    public String createDifference(String name, String baseName,
                                   String subtractName) {
        validateOperands(baseName, subtractName);
        requireFreshName(name);
        String tag = nextTag("dif");
        register(new SelectionDef(tag, name, "Difference",
                Collections.emptyMap(),
                Arrays.asList(baseName, subtractName), "selections"));
        return tag;
    }

    /**
     * Create an adjacent selection.
     * @param name            human-readable name
     * @param sourceName      name of the source selection
     * @param targetDimension dimension of adjacent entities (0-3)
     * @return generated tag, e.g., "adj1"
     */
    public String createAdjacent(String name, String sourceName,
                                 int targetDimension) {
        validateOperands(sourceName);
        Map<String, String> params = new LinkedHashMap<>();
        params.put("targetDimension", String.valueOf(targetDimension));
        requireFreshName(name);
        String tag = nextTag("adj");
        register(new SelectionDef(tag, name, "Adjacent", params,
                Collections.singletonList(sourceName), "selections"));
        return tag;
    }

    // ---- Lookup and utility methods ----

    /** Get the tag for a named selection. Throws if not found. */
    public String getTag(String name) {
        SelectionDef d = selections.get(name);
        if (d == null) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra.getTag: not found: " + name);
        }
        return d.tag;
    }

    /** Get the full SelectionDef for a name. Throws if not found. */
    public SelectionDef get(String name) {
        SelectionDef d = selections.get(name);
        if (d == null) {
            throw new IllegalArgumentException(
                    "SelectionAlgebra.get: not found: " + name);
        }
        return d;
    }

    /** Safe existence check. */
    public boolean exists(String name) {
        return name != null && selections.containsKey(name);
    }

    /** True if the selection is a primitive (not a boolean operation). */
    public boolean isPrimitive(String name) {
        SelectionDef d = get(name);
        String t = d.type;
        return t.equals("Box") || t.equals("Ball")
                || t.equals("Cylinder") || t.equals("Explicit");
    }

    /** True if the selection is a boolean operation. */
    public boolean isComposite(String name) {
        return !isPrimitive(name);
    }

    /**
     * For composite selections, recursively resolve to primitive tags.
     * Returns a flat list of all primitive selection tags that
     * contribute to the named composite.
     */
    public List<String> resolveOperands(String name) {
        SelectionDef d = get(name);
        if (isPrimitive(name)) {
            return Collections.singletonList(d.tag);
        }
        List<String> result = new ArrayList<>();
        for (String opName : d.operands) {
            result.addAll(resolveOperands(opName));
        }
        return result;
    }

    /** Number of registered selections. */
    public int size() {
        return selections.size();
    }

    // ---- Serialization ----

    /**
     * Serialize to JSON.
     * Format:
     *   {"selections":[{"tag":"boxsel1","name":"top","type":"Box",
     *     "params":{"zmin":"0.9"},"operands":[],"stage":"selections"}, ...],
     *    "counters":{"boxsel":1}}
     */
    public String serialize() {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"selections\":[");
        boolean first = true;
        for (SelectionDef d : selections.values()) {
            if (!first) sb.append(",");
            first = false;
            sb.append("{\"tag\":\"").append(escapeJson(d.tag))
              .append("\",\"name\":\"").append(escapeJson(d.name))
              .append("\",\"type\":\"").append(escapeJson(d.type))
              .append("\",\"params\":{");
            boolean pFirst = true;
            for (Map.Entry<String, String> p : d.params.entrySet()) {
                if (!pFirst) sb.append(",");
                pFirst = false;
                sb.append("\"").append(escapeJson(p.getKey()))
                  .append("\":\"").append(escapeJson(p.getValue()))
                  .append("\"");
            }
            sb.append("},\"operands\":[");
            for (int i = 0; i < d.operands.size(); i++) {
                if (i > 0) sb.append(",");
                sb.append("\"").append(escapeJson(d.operands.get(i)))
                  .append("\"");
            }
            sb.append("],\"stage\":\"").append(escapeJson(d.stage))
              .append("\"}");
        }
        sb.append("],\"counters\":{");
        first = true;
        for (Map.Entry<String, Integer> c : counters.entrySet()) {
            if (!first) sb.append(",");
            first = false;
            sb.append("\"").append(escapeJson(c.getKey()))
              .append("\":").append(c.getValue());
        }
        sb.append("}}");
        return sb.toString();
    }

    /**
     * Reconstruct a SelectionAlgebra from JSON.
     */
    public static SelectionAlgebra deserialize(String json) {
        SelectionAlgebra sa = new SelectionAlgebra();

        // Find the selections array
        int arrStart = json.indexOf("[");
        int arrEnd = findMatchingBracket(json, arrStart);
        String arrStr = json.substring(arrStart + 1, arrEnd);

        // Parse each selection object
        int pos = 0;
        while (pos < arrStr.length()) {
            int objStart = arrStr.indexOf("{", pos);
            if (objStart < 0) break;
            int objEnd = findMatchingBracket(arrStr, objStart);
            String objStr = arrStr.substring(objStart + 1, objEnd);

            String tag   = extractJsonString(objStr, "tag");
            String name  = extractJsonString(objStr, "name");
            String type  = extractJsonString(objStr, "type");
            String stage = extractJsonString(objStr, "stage");

            // Parse params sub-object
            Map<String, String> params = new LinkedHashMap<>();
            int paramsIdx = objStr.indexOf("\"params\"");
            if (paramsIdx >= 0) {
                int pObjStart = objStr.indexOf("{", paramsIdx);
                if (pObjStart >= 0) {
                    int pObjEnd = objStr.indexOf("}", pObjStart);
                    String pStr = objStr.substring(pObjStart + 1, pObjEnd);
                    parseStringMap(pStr, params);
                }
            }

            // Parse operands array
            List<String> operands = new ArrayList<>();
            int opsIdx = objStr.indexOf("\"operands\"");
            if (opsIdx >= 0) {
                int opsArrStart = objStr.indexOf("[", opsIdx);
                if (opsArrStart >= 0) {
                    int opsArrEnd = objStr.indexOf("]", opsArrStart);
                    String opsStr = objStr.substring(opsArrStart + 1, opsArrEnd);
                    parseStringArray(opsStr, operands);
                }
            }

            SelectionDef def = new SelectionDef(tag, name, type,
                    params, operands, stage);
            sa.selections.put(name, def);
            sa.tagIndex.put(tag, def);

            pos = objEnd + 1;
        }

        // Parse counters
        int countersStart = json.indexOf("{", arrEnd);
        if (countersStart >= 0) {
            int countersEnd = json.indexOf("}", countersStart);
            String cStr = json.substring(countersStart + 1, countersEnd);
            parseCounters(cStr, sa.counters);
        }

        return sa;
    }

    /**
     * Compact multi-line view for LLM context.
     * Format:
     *   boxsel1=top_surface (Box: zmin=0.9, zmax=1.1, condition=inside)
     *   uni1=all_surfaces (Union: top_surface, bottom_surface)
     */
    public String toCondensedView() {
        StringBuilder sb = new StringBuilder();
        for (SelectionDef d : selections.values()) {
            sb.append(d.tag).append("=").append(d.name)
              .append(" (").append(d.type).append(": ");
            if (!d.params.isEmpty()) {
                boolean first = true;
                for (Map.Entry<String, String> p : d.params.entrySet()) {
                    if (!first) sb.append(", ");
                    first = false;
                    sb.append(p.getKey()).append("=").append(p.getValue());
                }
            } else if (!d.operands.isEmpty()) {
                boolean first = true;
                for (String op : d.operands) {
                    if (!first) sb.append(", ");
                    first = false;
                    sb.append(op);
                }
            }
            sb.append(")\n");
        }
        return sb.toString();
    }

    // ---- JSON helpers ----

    private static String escapeJson(String s) {
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
                default:   sb.append(c);
            }
        }
        return sb.toString();
    }

    private static int findMatchingBracket(String s, int openPos) {
        char open = s.charAt(openPos);
        char close = (open == '[') ? ']' : '}';
        int depth = 1;
        for (int i = openPos + 1; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') {
                i = skipJsonString(s, i);
            } else if (c == open) {
                depth++;
            } else if (c == close) {
                depth--;
                if (depth == 0) return i;
            }
        }
        return s.length() - 1;
    }

    private static int skipJsonString(String s, int quotePos) {
        for (int i = quotePos + 1; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\') { i++; continue; }
            if (c == '"') return i;
        }
        return s.length() - 1;
    }

    private static String extractJsonString(String obj, String key) {
        String search = "\"" + key + "\":\"";
        int start = obj.indexOf(search);
        if (start < 0) return "";
        start += search.length();
        StringBuilder sb = new StringBuilder();
        for (int i = start; i < obj.length(); i++) {
            char c = obj.charAt(i);
            if (c == '\\' && i + 1 < obj.length()) {
                char next = obj.charAt(i + 1);
                switch (next) {
                    case '"':  sb.append('"');  break;
                    case '\\': sb.append('\\'); break;
                    case 'n':  sb.append('\n'); break;
                    case 'r':  sb.append('\r'); break;
                    case 't':  sb.append('\t'); break;
                    default:   sb.append(next);
                }
                i++;
            } else if (c == '"') {
                break;
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private static void parseStringMap(String s, Map<String, String> map) {
        // Format: "key1":"val1","key2":"val2"
        int pos = 0;
        while (pos < s.length()) {
            int keyStart = s.indexOf("\"", pos);
            if (keyStart < 0) break;
            int keyEnd = s.indexOf("\"", keyStart + 1);
            String key = s.substring(keyStart + 1, keyEnd);

            int valStart = s.indexOf("\"", keyEnd + 1);
            if (valStart < 0) break;
            int valEnd = valStart + 1;
            // Handle escaped quotes in values
            while (valEnd < s.length()) {
                char c = s.charAt(valEnd);
                if (c == '\\') { valEnd += 2; continue; }
                if (c == '"') break;
                valEnd++;
            }
            String val = s.substring(valStart + 1, valEnd);
            map.put(key, val);
            pos = valEnd + 1;
        }
    }

    private static void parseStringArray(String s, List<String> list) {
        // Format: "val1","val2"
        int pos = 0;
        while (pos < s.length()) {
            int start = s.indexOf("\"", pos);
            if (start < 0) break;
            int end = start + 1;
            while (end < s.length()) {
                char c = s.charAt(end);
                if (c == '\\') { end += 2; continue; }
                if (c == '"') break;
                end++;
            }
            list.add(s.substring(start + 1, end));
            pos = end + 1;
        }
    }

    private static void parseCounters(String s, Map<String, Integer> map) {
        int pos = 0;
        while (pos < s.length()) {
            int keyStart = s.indexOf("\"", pos);
            if (keyStart < 0) break;
            int keyEnd = s.indexOf("\"", keyStart + 1);
            String key = s.substring(keyStart + 1, keyEnd);

            int colonPos = s.indexOf(":", keyEnd);
            int valueStart = colonPos + 1;
            int valueEnd = valueStart;
            while (valueEnd < s.length()
                    && (Character.isDigit(s.charAt(valueEnd))
                        || s.charAt(valueEnd) == '-'
                        || s.charAt(valueEnd) == ' ')) {
                valueEnd++;
            }
            int value = Integer.parseInt(
                    s.substring(valueStart, valueEnd).trim());
            map.put(key, value);
            pos = valueEnd;
        }
    }
}
