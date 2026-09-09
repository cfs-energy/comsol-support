/**
 * TagRegistry — deterministic tag assignment for COMSOL model-tree nodes.
 *
 * Pure Java (no com.comsol.model.* imports). Tags are type-prefixed
 * auto-incrementing strings (e.g., "blk1", "cyl2"). Human-readable names
 * map to tags for safe lookup. Serializes to JSON for compaction
 * working_memory persistence.
 *
 * Standard prefixes:
 *   blk  = Block          cyl  = Cylinder       sph  = Sphere
 *   wp   = WorkPlane      ext  = Extrude        rev  = Revolve
 *   con  = Cone           tor  = Torus          arr  = Array
 *   imp  = Import         prt  = Partition       mir  = Mirror
 *   comp = Component      geom = Geometry        par  = Parameter
 *   fn   = Function       var  = Variable        mat  = Material
 *   phys = Physics        mesh = Mesh            stdy = Study
 *   sol  = Solver         pp   = PostProcessing
 */

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class TagRegistry {

    // ---- Inner class ----

    public static class TagEntry {
        public final String tag;
        public final String name;
        public final String type;
        public final String parent;
        public final String stage;

        public TagEntry(String tag, String name, String type,
                        String parent, String stage) {
            this.tag = tag;
            this.name = name;
            this.type = type;
            this.parent = parent;
            this.stage = stage;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (!(o instanceof TagEntry)) return false;
            TagEntry that = (TagEntry) o;
            return tag.equals(that.tag) && name.equals(that.name);
        }

        @Override
        public int hashCode() {
            return tag.hashCode() * 31 + name.hashCode();
        }

        @Override
        public String toString() {
            return tag + "=" + name + " (" + type + ", parent=" + parent
                    + ", stage=" + stage + ")";
        }
    }

    // ---- State ----

    private final Map<String, TagEntry> entries;      // name -> entry
    private final Map<String, TagEntry> tagIndex;     // tag  -> entry
    private final Map<String, Integer>  counters;     // prefix -> next counter

    // ---- Constructor ----

    public TagRegistry() {
        this.entries  = new LinkedHashMap<>();
        this.tagIndex = new LinkedHashMap<>();
        this.counters = new HashMap<>();
    }

    // ---- Core methods ----

    /**
     * Create a new tag entry. Returns the generated tag string.
     *
     * @param name   human-readable name (must be unique, non-null, non-empty)
     * @param prefix tag prefix (e.g., "blk"); must be non-null, non-empty
     * @param type   COMSOL node type (e.g., "Block")
     * @param parent parent tag (e.g., "geom1") — may be empty, not null
     * @param stage  pipeline stage (e.g., "geometry")
     * @return generated tag, e.g., "blk1"
     */
    public String create(String name, String prefix, String type,
                         String parent, String stage) {
        if (name == null || name.isEmpty()) {
            throw new IllegalArgumentException(
                    "TagRegistry.create: name must be non-null and non-empty");
        }
        if (prefix == null || prefix.isEmpty()) {
            throw new IllegalArgumentException(
                    "TagRegistry.create: prefix must be non-null and non-empty");
        }
        if (type == null)   type = "";
        if (parent == null) parent = "";
        if (stage == null)  stage = "";

        if (entries.containsKey(name)) {
            throw new IllegalArgumentException(
                    "TagRegistry.create: name already exists: " + name);
        }

        int counter = counters.getOrDefault(prefix, 0) + 1;
        counters.put(prefix, counter);

        String tag = prefix + counter;
        TagEntry entry = new TagEntry(tag, name, type, parent, stage);
        entries.put(name, entry);
        tagIndex.put(tag, entry);
        return tag;
    }

    /** Get the tag for a given name. Throws if not found. */
    public String get(String name) {
        TagEntry e = entries.get(name);
        if (e == null) {
            throw new IllegalArgumentException(
                    "TagRegistry.get: name not found: " + name);
        }
        return e.tag;
    }

    /** Get the full TagEntry for a given name. Throws if not found. */
    public TagEntry getEntry(String name) {
        TagEntry e = entries.get(name);
        if (e == null) {
            throw new IllegalArgumentException(
                    "TagRegistry.getEntry: name not found: " + name);
        }
        return e;
    }

    /** Reverse lookup: tag -> entry. Throws if not found. */
    public TagEntry getByTag(String tag) {
        TagEntry e = tagIndex.get(tag);
        if (e == null) {
            throw new IllegalArgumentException(
                    "TagRegistry.getByTag: tag not found: " + tag);
        }
        return e;
    }

    /** Safe existence check by name. */
    public boolean exists(String name) {
        return name != null && entries.containsKey(name);
    }

    /** Safe existence check by tag. */
    public boolean tagExists(String tag) {
        return tag != null && tagIndex.containsKey(tag);
    }

    /** All entries for a given stage. */
    public List<TagEntry> getByStage(String stage) {
        List<TagEntry> result = new ArrayList<>();
        for (TagEntry e : entries.values()) {
            if (e.stage.equals(stage)) {
                result.add(e);
            }
        }
        return result;
    }

    /** All entries of a given type. */
    public List<TagEntry> getByType(String type) {
        List<TagEntry> result = new ArrayList<>();
        for (TagEntry e : entries.values()) {
            if (e.type.equals(type)) {
                result.add(e);
            }
        }
        return result;
    }

    /** All entries whose parent matches the given name's tag. */
    public List<TagEntry> getChildren(String parentName) {
        String parentTag = get(parentName);  // throws if not found
        List<TagEntry> result = new ArrayList<>();
        for (TagEntry e : entries.values()) {
            if (e.parent.equals(parentTag)) {
                result.add(e);
            }
        }
        return result;
    }

    /** Number of registered tags. */
    public int size() {
        return entries.size();
    }

    // ---- Serialization ----

    /**
     * Serialize the full registry state to JSON.
     * Format:
     *   {"entries":[{"tag":"blk1","name":"main_block","type":"Block",
     *     "parent":"geom1","stage":"geometry"}, ...],
     *    "counters":{"blk":2,"cyl":1}}
     */
    public String serialize() {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"entries\":[");
        boolean first = true;
        for (TagEntry e : entries.values()) {
            if (!first) sb.append(",");
            first = false;
            sb.append("{\"tag\":\"").append(escapeJson(e.tag))
              .append("\",\"name\":\"").append(escapeJson(e.name))
              .append("\",\"type\":\"").append(escapeJson(e.type))
              .append("\",\"parent\":\"").append(escapeJson(e.parent))
              .append("\",\"stage\":\"").append(escapeJson(e.stage))
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
     * Reconstruct a TagRegistry from its JSON representation.
     */
    public static TagRegistry deserialize(String json) {
        TagRegistry reg = new TagRegistry();
        // Parse entries array
        int entriesStart = json.indexOf("[");
        int entriesEnd = findMatchingBracket(json, entriesStart);
        String entriesStr = json.substring(entriesStart + 1, entriesEnd);

        // Parse each entry object
        int pos = 0;
        while (pos < entriesStr.length()) {
            int objStart = entriesStr.indexOf("{", pos);
            if (objStart < 0) break;
            int objEnd = findMatchingBracket(entriesStr, objStart);
            String objStr = entriesStr.substring(objStart + 1, objEnd);

            String tag    = extractJsonString(objStr, "tag");
            String name   = extractJsonString(objStr, "name");
            String type   = extractJsonString(objStr, "type");
            String parent = extractJsonString(objStr, "parent");
            String stage  = extractJsonString(objStr, "stage");

            TagEntry entry = new TagEntry(tag, name, type, parent, stage);
            reg.entries.put(name, entry);
            reg.tagIndex.put(tag, entry);

            pos = objEnd + 1;
        }

        // Parse counters object
        int countersStart = json.indexOf("{", entriesEnd);
        if (countersStart >= 0) {
            int countersEnd = json.indexOf("}", countersStart);
            String countersStr = json.substring(countersStart + 1, countersEnd);
            parseCounters(countersStr, reg.counters);
        }

        return reg;
    }

    /**
     * Compact multi-line view for LLM context injection.
     * Format:
     *   [geometry] blk1=main_block (Block, parent=geom1)
     */
    public String toCondensedView() {
        StringBuilder sb = new StringBuilder();
        for (TagEntry e : entries.values()) {
            sb.append("[").append(e.stage).append("] ")
              .append(e.tag).append("=").append(e.name)
              .append(" (").append(e.type)
              .append(", parent=").append(e.parent).append(")\n");
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
                // Skip string contents
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

    private static void parseCounters(String s, Map<String, Integer> map) {
        // Format: "blk":2,"cyl":1
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
                        || s.charAt(valueEnd) == '-')) {
                valueEnd++;
            }
            int value = Integer.parseInt(s.substring(valueStart, valueEnd).trim());
            map.put(key, value);
            pos = valueEnd;
        }
    }
}
