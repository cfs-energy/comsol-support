/**
 * CorpusBatchConverter — batch convert .mph application models to .java source.
 *
 * Unlike ModelExporter (single-command, single-model), this class initializes
 * COMSOL once and converts all models sequentially. Designed for mining the
 * 875+ application models in the COMSOL examples library.
 *
 * Usage:
 *   java CorpusBatchConverter <mph_list_file> <output_dir> [--resume]
 *
 * mph_list_file: text file with one absolute .mph path per line
 * output_dir: where to write .java files (directory structure mirrored)
 *
 * Output protocol: JSON on stdout, one object per line:
 *   {"success":true,  "mph":"...", "java":"...", "index":N, "total":M}
 *   {"success":false, "mph":"...", "error":"...", "index":N, "total":M}
 *   {"done":true, "converted":X, "failed":Y, "total":Z}
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.io.*;
import java.nio.file.*;
import java.util.*;

public class CorpusBatchConverter {

    public static void main(String[] args) {
        if (args.length < 2) {
            emitError("Usage: CorpusBatchConverter <mph_list_file> <output_dir> [--resume]");
            return;
        }

        String listFile = args[0];
        String outputDir = args[1];
        boolean resume = args.length > 2 && "--resume".equals(args[2]);

        List<String> mphPaths;
        try {
            mphPaths = Files.readAllLines(Paths.get(listFile));
            // Remove empty lines
            mphPaths.removeIf(String::isEmpty);
        } catch (IOException e) {
            emitError("Cannot read list file: " + e.getMessage());
            return;
        }

        int total = mphPaths.size();
        int converted = 0;
        int failed = 0;

        // Initialize COMSOL standalone — one-time cost
        try {
            ModelUtil.initStandalone(false);
        } catch (Exception e) {
            emitError("Failed to initialize COMSOL: " + e.getMessage());
            return;
        }

        try {
            for (int i = 0; i < total; i++) {
                String mphPath = mphPaths.get(i).trim();
                if (mphPath.isEmpty()) continue;

                String javaPath = computeOutputPath(mphPath, outputDir);

                // Resume: skip if .java already exists
                if (resume && Files.exists(Paths.get(javaPath))) {
                    converted++;
                    continue;
                }

                try {
                    // Ensure output directory exists
                    Files.createDirectories(Paths.get(javaPath).getParent());

                    // Load model
                    String tag = "Model_" + i;
                    Model model = ModelUtil.load(tag, mphPath);

                    // Save as a Model Java-file. The explicit two-arg
                    // form is required: one-arg save() ignores the
                    // extension and silently writes a binary .mph
                    // under the .java name (measured on 6.4.0.429).
                    model.save(javaPath, "java");
                    // Some releases append ".java" to the supplied
                    // filename; normalize so downstream *.java globs
                    // find the file either way.
                    Path doubled = Paths.get(javaPath + ".java");
                    if (!Files.exists(Paths.get(javaPath))
                            && Files.exists(doubled)) {
                        Files.move(doubled, Paths.get(javaPath));
                    }

                    // Release memory
                    ModelUtil.remove(tag);

                    converted++;
                    emitProgress(true, mphPath, javaPath, null, i + 1, total);
                } catch (Exception e) {
                    failed++;
                    emitProgress(false, mphPath, javaPath, e.getMessage(), i + 1, total);
                }
            }
        } finally {
            // Final summary
            emitDone(converted, failed, total);
            // COMSOL's non-daemon threads keep the JVM alive after main
            // returns, and ModelUtil.disconnect() hangs (see
            // docs/known-gotchas.md) — force-exit once the summary is
            // out, same pattern as ModelExporter/SolverTelemetry.
            // halt() skips shutdown hooks, so flush explicitly first.
            System.out.flush();
            System.err.flush();
            Runtime.getRuntime().halt(0);
        }
    }

    /**
     * Compute output .java path mirroring the input directory structure.
     * Input:  <COMSOL_PATH>/applications/Heat_Transfer/model.mph
     * Output: <outputDir>/Heat_Transfer/model.java
     */
    private static String computeOutputPath(String mphPath, String outputDir) {
        Path mph = Paths.get(mphPath);
        String filename = mph.getFileName().toString();
        // Replace .mph extension with .java
        String javaName = filename.replaceAll("\\.mph$", ".java");

        // Try to extract relative path from "applications/" onwards.
        // Normalize separators first — Windows paths use backslashes,
        // which previously missed the match and flattened the mirror.
        String pathStr = mph.toString().replace('\\', '/');
        int appIdx = pathStr.indexOf("applications/");
        if (appIdx >= 0) {
            String relative = pathStr.substring(appIdx + "applications/".length());
            Path relDir = Paths.get(relative).getParent();
            if (relDir != null) {
                return Paths.get(outputDir, relDir.toString(), javaName).toString();
            }
        }

        // Fallback: use parent directory name
        Path parent = mph.getParent();
        String dirName = (parent != null) ? parent.getFileName().toString() : "unknown";
        return Paths.get(outputDir, dirName, javaName).toString();
    }

    private static void emitProgress(boolean success, String mph, String java,
                                      String error, int index, int total) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"success\":").append(success);
        sb.append(",\"mph\":\"").append(escapeJson(mph)).append("\"");
        sb.append(",\"java\":\"").append(escapeJson(java)).append("\"");
        if (error != null) {
            sb.append(",\"error\":\"").append(escapeJson(error)).append("\"");
        }
        sb.append(",\"index\":").append(index);
        sb.append(",\"total\":").append(total);
        sb.append("}");
        System.out.println(sb.toString());
        System.out.flush();
    }

    private static void emitDone(int converted, int failed, int total) {
        System.out.println("{\"done\":true,\"converted\":" + converted +
                           ",\"failed\":" + failed + ",\"total\":" + total + "}");
        System.out.flush();
    }

    private static void emitError(String message) {
        System.out.println("{\"success\":false,\"error\":\"" +
                           escapeJson(message) + "\"}");
        System.out.flush();
    }

    private static String escapeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
