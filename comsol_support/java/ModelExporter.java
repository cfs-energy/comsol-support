/**
 * ModelExporter — produce a saved .mph either by building from a
 * `buildModel`-contract Java class or by loading an existing .mph and
 * optionally applying a `mutate`-contract Java class. Shares one
 * solve/save/telemetry path so both modes get identical instrumentation.
 *
 * Sibling to CorpusBatchConverter (batch .mph -> .java).
 *
 * Two source modes, mutually exclusive:
 *
 *   BUILD MODE (mphgen entry point)
 *     Reflect on a builder class, call its static
 *     `public static Model buildModel(Map<String,String> args)`,
 *     save the returned Model.
 *
 *   EDIT MODE (edit-mph entry point)
 *     ModelUtil.load(...) an existing .mph. If a mutator class is named,
 *     reflect on its
 *     `public static Model mutate(Model m, Map<String,String> args)`
 *     and call it. The mutator returns the (possibly identical) Model
 *     to save. If no mutator is named, the model is loaded and re-saved
 *     unchanged (useful for solving an existing .mph).
 *
 * Usage:
 *   java ModelExporter
 *     (--builder-class <ClassName> | --input <existing.mph> [--mutator-class <Name>])
 *     --output <path.mph>
 *     [--arg key=value] ...
 *     [--solve <study_tag>]       # run the named study before saving
 *
 * Output:
 *   - One or more TELEMETRY: JSON-line events on stdout (see
 *     SolverTelemetry) at waypoints: run_start, build_done, solve_start,
 *     solve_done, save_done, partial_save, halt. Model-agnostic.
 *   - Final JSON envelope line on stdout (success or error) — preserved
 *     for backward compatibility with mphgen.
 *   - Exit 0 on success, 1 on error.
 *
 * Save-on-exit: whenever the Model has been constructed, the harness
 * attempts a partial save to "<output>.partial.mph" on any failure path
 * (solver error, main-save error, or other exception). This preserves
 * diagnostic signal from halted solves rather than discarding it on
 * the exception path.
 *
 * Does NOT call ModelUtil.disconnect() — that call hangs indefinitely in
 * headless mode; process teardown releases resources. System.exit(0)
 * is the exit path.
 */

import com.comsol.model.*;
import com.comsol.model.util.*;

import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

public class ModelExporter {

    /** Upper bound on how long attemptPartialSave will wait for
     *  model.save() to complete before emitting partial_save_timeout and
     *  giving up. Bounds the hang risk on pathological failed-solve
     *  states while still allowing a normal partial save to finish. */
    private static final long PARTIAL_SAVE_TIMEOUT_MS = 30_000L;

    /** Heartbeat interval for solver_heartbeat events while study.run()
     *  is in flight. Gives consumers "still alive" feedback at a cadence
     *  dense enough to watch but sparse enough not to bloat the sidecar
     *  or SQLite telemetry table on long solves. */
    private static final long HEARTBEAT_INTERVAL_MS = 2_000L;

    /** Set immediately after the finally-block halt event so the
     *  last-gasp shutdown hook only fires when the JVM is
     *  torn down BEFORE the normal halt — e.g. SIGTERM from a harness
     *  timeout kill. Volatile: written by main, read by the hook thread. */
    private static volatile boolean haltEmitted = false;

    /** Per-tag cap on result series row count when emitting result_probe
     *  or result_global events. Keeps one runaway probe from bloating the
     *  event stream; consumers that need raw data should read the .mph. */
    private static final int RESULT_SERIES_MAX_ROWS = 1000;

    /** Pre-rendered JSON array of COMSOL's detailed diagnostic lines for
     *  the most recent failure (see {@link #errorDetail}). Defaults to an
     *  empty array; set at each exception sink so the terminal `halt`
     *  event can carry the same detail as the failure envelope. Single
     *  run per JVM, so a static field is safe. */
    private static String errorDetailJson = "[]";

    public static void main(String[] args) {
        String builderClass = null;
        String inputPath = null;
        String mutatorClass = null;
        String outputPath = null;
        String solveStudy = null;
        String meshTag = null;
        String queryClass = null;
        boolean noSave = false;
        long licenseTimeoutMs = 0L;  // 0 = wait indefinitely (legacy default)
        Map<String,String> builderArgs = new HashMap<>();

        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if ("--builder-class".equals(a) && i + 1 < args.length) {
                builderClass = args[++i];
            } else if ("--input".equals(a) && i + 1 < args.length) {
                inputPath = args[++i];
            } else if ("--mutator-class".equals(a) && i + 1 < args.length) {
                mutatorClass = args[++i];
            } else if ("--output".equals(a) && i + 1 < args.length) {
                outputPath = args[++i];
            } else if ("--solve".equals(a) && i + 1 < args.length) {
                solveStudy = args[++i];
            } else if ("--mesh".equals(a) && i + 1 < args.length) {
                meshTag = args[++i];
            } else if ("--query-class".equals(a) && i + 1 < args.length) {
                queryClass = args[++i];
                noSave = true;  // a query never mutates/persists the model
            } else if ("--no-save".equals(a)) {
                noSave = true;
            } else if ("--license-timeout".equals(a) && i + 1 < args.length) {
                // Seconds; bounds the license checkout (initStandalone +
                // load) so a seatless run fails fast instead of parking
                // with partial checkouts. 0 / negative = disabled.
                try {
                    licenseTimeoutMs = Math.max(0L,
                            (long) (Double.parseDouble(args[++i]) * 1000.0));
                } catch (NumberFormatException nfe) {
                    emitError("Bad --license-timeout value; expected seconds");
                    System.exit(1);
                }
            } else if ("--arg".equals(a) && i + 1 < args.length) {
                String kv = args[++i];
                int eq = kv.indexOf('=');
                if (eq < 1) {
                    emitError("Bad --arg value '" + kv + "'; expected key=value");
                    System.exit(1);
                }
                builderArgs.put(kv.substring(0, eq), kv.substring(eq + 1));
            } else {
                emitError("Unknown argument: " + a);
                System.exit(1);
            }
        }

        if (outputPath == null && !noSave) {
            emitError("Required: --output <path.mph> "
                    + "(may be omitted only with --no-save / --query-class)");
            System.exit(1);
        }
        boolean buildMode = builderClass != null;
        boolean editMode = inputPath != null;
        if (buildMode == editMode) {
            emitError("Specify exactly one of --builder-class <name> or "
                    + "--input <existing.mph>");
            System.exit(1);
        }
        if (mutatorClass != null && !editMode) {
            emitError("--mutator-class requires --input");
            System.exit(1);
        }

        SolverTelemetry.reset();
        // Last-gasp forensics: if the JVM is torn down
        // before the finally-block halt event (SIGTERM from a harness
        // timeout kill, an explicit System.exit in third-party code),
        // emit a jvm_shutdown event so the sidecar records WHY the
        // stream ended instead of just stopping mid-heartbeat. SIGKILL
        // and native aborts (SIGSEGV/SIGABRT) cannot run hooks — for
        // those the Python side decodes the negative exit code and
        // looks for hs_err files (see G-SIGABRT-NO-HSERR).
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            if (!haltEmitted) {
                try {
                    SolverTelemetry.emit("jvm_shutdown",
                        SolverTelemetry.payloadMixed(
                            "reason", "shutdown_before_halt",
                            "rss_bytes", SolverTelemetry.processRssBytes()));
                } catch (Throwable ignored) {
                    // Never fail the shutdown sequence over telemetry.
                }
            }
        }, "telemetry-last-gasp"));
        SolverTelemetry.emit("run_start", SolverTelemetry.payload(
                "mode", buildMode ? "build" : "edit",
                "builder_class", builderClass == null ? "" : builderClass,
                "input", inputPath == null ? "" : inputPath,
                "mutator_class", mutatorClass == null ? "" : mutatorClass,
                "output", outputPath,
                "solve_study", solveStudy == null ? "" : solveStudy));

        long t0 = System.currentTimeMillis();
        Model model = null;
        String haltReason = "success";
        String haltMessage = "";
        boolean solved = false;
        boolean meshRan = false;
        boolean meshSuccess = false;
        boolean mainSaveDone = false;
        // Effectively-final copies for capture by the license-guard lambdas.
        final long licTimeoutMs = licenseTimeoutMs;
        final String inPath = inputPath;

        try {
            runBounded(() -> { ModelUtil.initStandalone(false); return null; },
                    licTimeoutMs, "ModelUtil.initStandalone");

            if (buildMode) {
                // --- BUILD MODE: reflect on builderClass.buildModel ---------
                Class<?> cls;
                try {
                    cls = Class.forName(builderClass);
                } catch (ClassNotFoundException e) {
                    haltReason = "contract_error";
                    haltMessage = "Builder class not found: " + builderClass;
                    emitError(haltMessage);
                    return;
                }

                Method m;
                try {
                    m = cls.getMethod("buildModel", Map.class);
                } catch (NoSuchMethodException e) {
                    haltReason = "contract_error";
                    haltMessage = "Builder '" + builderClass + "' must expose "
                            + "public static Model buildModel(Map<String,String>). "
                            + "See docs/mphgen.md for the contract.";
                    emitError(haltMessage);
                    return;
                }

                Object result;
                try {
                    result = m.invoke(null, builderArgs);
                } catch (Exception e) {
                    haltReason = "build_error";
                    Throwable cause = e.getCause() != null ? e.getCause() : e;
                    haltMessage = cause.getMessage() != null
                            ? cause.getMessage() : cause.getClass().getName();
                    emitException(cause);
                    return;
                }

                if (!(result instanceof Model)) {
                    haltReason = "contract_error";
                    haltMessage = "buildModel returned "
                            + (result == null ? "null" : result.getClass().getName())
                            + "; expected com.comsol.model.Model";
                    emitError(haltMessage);
                    return;
                }
                model = (Model) result;
                SolverTelemetry.emit("build_done", SolverTelemetry.payload(
                        "builder_class", builderClass));
            } else {
                // --- EDIT MODE: load existing .mph, optionally mutate -------
                try {
                    model = runBounded(() -> ModelUtil.load("loaded", inPath),
                            licTimeoutMs, "ModelUtil.load");
                } catch (LicenseTimeoutException lte) {
                    throw lte;  // handled by the outer license_timeout catch
                } catch (Exception e) {
                    haltReason = "load_error";
                    Throwable cause = e.getCause() != null ? e.getCause() : e;
                    haltMessage = "ModelUtil.load failed for '"
                            + inputPath + "': "
                            + (cause.getMessage() != null
                                    ? cause.getMessage()
                                    : cause.getClass().getName());
                    emitException(cause);
                    return;
                }
                SolverTelemetry.emit("load_done", SolverTelemetry.payload(
                        "input", inputPath));

                if (mutatorClass != null) {
                    Class<?> mcls;
                    try {
                        mcls = Class.forName(mutatorClass);
                    } catch (ClassNotFoundException e) {
                        haltReason = "contract_error";
                        haltMessage = "Mutator class not found: " + mutatorClass;
                        emitError(haltMessage);
                        return;
                    }
                    Method mm;
                    try {
                        mm = mcls.getMethod("mutate", Model.class, Map.class);
                    } catch (NoSuchMethodException e) {
                        haltReason = "contract_error";
                        haltMessage = "Mutator '" + mutatorClass + "' must expose "
                                + "public static Model mutate(Model, "
                                + "Map<String,String>). See docs/mphedit.md.";
                        emitError(haltMessage);
                        return;
                    }
                    Object mres;
                    try {
                        mres = mm.invoke(null, model, builderArgs);
                    } catch (Exception e) {
                        haltReason = "mutate_error";
                        Throwable cause = e.getCause() != null ? e.getCause() : e;
                        haltMessage = cause.getMessage() != null
                                ? cause.getMessage()
                                : cause.getClass().getName();
                        emitException(cause);
                        return;
                    }
                    if (!(mres instanceof Model)) {
                        haltReason = "contract_error";
                        haltMessage = "mutate returned "
                                + (mres == null ? "null" : mres.getClass().getName())
                                + "; expected com.comsol.model.Model";
                        emitError(haltMessage);
                        return;
                    }
                    model = (Model) mres;
                    SolverTelemetry.emit("mutate_done", SolverTelemetry.payload(
                            "mutator_class", mutatorClass));
                }
            }

            // ---- First-class mesh build (--mesh <tag>) ----
            // Runs the WHOLE mesh sequence (the only reliable commit
            // point — G-MESHRUN-TAG-RERUNS-SEQUENCE) with mesh_heartbeat
            // liveness + RSS, swallows a build exception so the partial
            // mesh still saves (G-MESH-THROW-COMMITS-PARTIAL) while
            // reporting it as a mesh_error event, and always emits a
            // post-run element census (mesh_census) plus per-feature
            // build records (mesh_feature_build) — "score the artifact,
            // not the exception". See docs/meshing.md.
            if (meshTag != null) {
                int meshRc = runMeshSequence(model, meshTag);
                if (meshRc == MESH_TAG_NOT_FOUND) {
                    haltReason = "mesh_error";
                    haltMessage = "Mesh sequence '" + meshTag
                            + "' not found in any component";
                    emitError(haltMessage);
                    return;
                }
                meshRan = true;
                meshSuccess = (meshRc == MESH_OK);
            }

            if (solveStudy != null) {
                SolverTelemetry.emit("solve_start", SolverTelemetry.payload(
                        "study_tag", solveStudy));
                final long solveStart = System.currentTimeMillis();
                final String studyTag = solveStudy;

                // Tee stdout/stderr so every non-TELEMETRY line COMSOL
                // prints during the solve is additionally emitted as a
                // structured solver_info event (queryable via SQLite /
                // MCP). Raw bytes still reach the original stream, so
                // the --stream mirror and existing log behavior are
                // unchanged.
                final java.io.PrintStream origOut = System.out;
                final java.io.PrintStream origErr = System.err;
                final SolverStdoutTee teeOut = new SolverStdoutTee(origOut);
                final SolverStdoutTee teeErr = new SolverStdoutTee(origErr);
                System.setOut(teeOut);
                System.setErr(teeErr);

                // One outer try/finally over BOTH the heartbeat
                // scheduler creation and the solve, so any exception
                // between setOut and shutdown (even during executor
                // construction) still restores the original streams and
                // cleans up partial resources. Without this, a
                // SecurityException from Executors.new... would leak
                // the tee on System.out for the rest of the JVM.
                ScheduledExecutorService heart = null;
                ScheduledFuture<?> beat = null;
                try {
                    heart = Executors.newSingleThreadScheduledExecutor(
                        new ThreadFactory() {
                            @Override
                            public Thread newThread(Runnable r) {
                                Thread t = new Thread(r, "solver-heartbeat");
                                t.setDaemon(true);
                                return t;
                            }
                        });
                    beat = heart.scheduleAtFixedRate(() -> {
                        try {
                            long elapsed = System.currentTimeMillis()
                                    - solveStart;
                            // Memory signal: rss_bytes is
                            // Linux VmRSS (-1 when unavailable); heap
                            // fields are JVM-view. Lets a post-mortem
                            // distinguish "grew until killed" from
                            // "died flat" — the question the
                            // original meshing campaign could not answer.
                            Runtime rt = Runtime.getRuntime();
                            SolverTelemetry.emit(
                                "solver_heartbeat",
                                SolverTelemetry.payloadMixed(
                                    "study_tag", studyTag,
                                    "elapsed_solve_ms", elapsed,
                                    "rss_bytes",
                                    SolverTelemetry.processRssBytes(),
                                    "jvm_heap_used_bytes",
                                    rt.totalMemory() - rt.freeMemory(),
                                    "jvm_heap_max_bytes", rt.maxMemory()));
                        } catch (Throwable ignored) {
                            // Never let a telemetry emit blow up the
                            // solve.
                        }
                    }, HEARTBEAT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS,
                       TimeUnit.MILLISECONDS);
                    try {
                        model.study(solveStudy).run();
                        solved = true;
                        long solveMs = System.currentTimeMillis() - solveStart;
                        SolverTelemetry.emit("solve_done",
                            SolverTelemetry.payloadMixed(
                                "study_tag", solveStudy,
                                "status", "success",
                                "elapsed_solve_ms", solveMs));
                    } catch (Exception e) {
                        haltReason = "solver_error";
                        Throwable cause = e.getCause() != null
                                ? e.getCause() : e;
                        haltMessage = "Study '" + solveStudy + "' failed: "
                                + (cause.getMessage() != null
                                        ? cause.getMessage()
                                        : cause.getClass().getName());
                        // Harvest COMSOL's detailed diagnostic from the
                        // full exception chain (not just the generic
                        // getMessage), and carry it on both solve_done
                        // and the terminal halt event.
                        errorDetailJson = jsonStringArray(errorDetail(e));
                        long solveMs = System.currentTimeMillis() - solveStart;
                        SolverTelemetry.emit("solve_done",
                            SolverTelemetry.payloadMixed(
                                "study_tag", solveStudy,
                                "status", "error",
                                "error", haltMessage,
                                "error_detail",
                                new SolverTelemetry.Raw(errorDetailJson),
                                "elapsed_solve_ms", solveMs));
                        emitError(haltMessage);
                        return;
                    }
                } finally {
                    // Order: cancel scheduler first so no stray beat
                    // fires while we're tearing down the tee, flush any
                    // partial line, then restore streams. Restoration
                    // happens even if scheduler creation itself threw.
                    if (beat != null) beat.cancel(true);
                    if (heart != null) heart.shutdownNow();
                    try { teeOut.flushRemaining(); } catch (Throwable ignored) {}
                    try { teeErr.flushRemaining(); } catch (Throwable ignored) {}
                    System.setOut(origOut);
                    System.setErr(origErr);
                }

                // Extract post-solve numerical results (probes + globals)
                // into the telemetry stream so the caller can inspect
                // headline outputs without reopening the .mph. Defensive:
                // per-tag and whole-block try/catch so extraction never
                // prevents the model from being saved.
                extractResults(model);
            }

            // --- QUERY MODE: run a read-only introspection class ---------
            // Sibling to the mutate contract. The class exposes
            //   public static Map<String,Object> query(Model, Map<String,String>)
            // and returns arbitrary JSON-serializable data (selection
            // membership, mesh counts, entity coords, expression evals —
            // whatever the query author chooses). Topic-agnostic: this
            // path neither knows nor cares what is being inspected. Always
            // implies no-save.
            String queryResultJson = null;
            if (queryClass != null) {
                Class<?> qcls;
                try {
                    qcls = Class.forName(queryClass);
                } catch (ClassNotFoundException e) {
                    haltReason = "contract_error";
                    haltMessage = "Query class not found: " + queryClass;
                    emitError(haltMessage);
                    return;
                }
                Method qm;
                try {
                    qm = qcls.getMethod("query", Model.class, Map.class);
                } catch (NoSuchMethodException e) {
                    haltReason = "contract_error";
                    haltMessage = "Query '" + queryClass + "' must expose "
                            + "public static Map<String,Object> "
                            + "query(Model, Map<String,String>). "
                            + "See docs/query-mph.md.";
                    emitError(haltMessage);
                    return;
                }
                try {
                    Object qres = qm.invoke(null, model, builderArgs);
                    // Always wrap to a JSON object so the telemetry/event
                    // payload shape stays {object}; a non-Map result is
                    // nested under "value".
                    queryResultJson = (qres instanceof Map)
                            ? jsonOf(qres)
                            : "{\"value\":" + jsonOf(qres) + "}";
                    SolverTelemetry.emit("query_result", queryResultJson);
                } catch (Exception e) {
                    haltReason = "query_error";
                    Throwable cause = e.getCause() != null ? e.getCause() : e;
                    haltMessage = "query(" + queryClass + ") failed: "
                            + (cause.getMessage() != null
                                    ? cause.getMessage()
                                    : cause.getClass().getName());
                    emitException(cause);
                    return;
                }
            }

            if (noSave) {
                // Read-only / discard path — never write the (possibly
                // multi-GB) model. mainSaveDone stays conceptually "done"
                // so the finally block does not attempt a partial save.
                SolverTelemetry.emit("save_skipped", SolverTelemetry.payload(
                        "reason", queryClass != null ? "query" : "no_save"));
            } else {
                try {
                    model.save(outputPath);
                    mainSaveDone = true;
                    SolverTelemetry.emit("save_done", SolverTelemetry.payload(
                            "path", outputPath));
                } catch (Exception e) {
                    haltReason = "save_error";
                    Throwable cause = e.getCause() != null ? e.getCause() : e;
                    haltMessage = "model.save failed: "
                            + (cause.getMessage() != null
                                    ? cause.getMessage()
                                    : cause.getClass().getName());
                    emitException(cause);
                    return;
                }
            }

            long elapsedMs = System.currentTimeMillis() - t0;
            emit("{\"success\":true,"
                    + "\"mode\":\"" + (buildMode ? "build" : "edit") + "\","
                    + "\"output\":" + (outputPath == null ? "null"
                            : "\"" + escapeJson(outputPath) + "\"") + ","
                    + "\"saved\":" + (!noSave) + ","
                    + "\"builder\":" + (builderClass == null ? "null"
                            : "\"" + escapeJson(builderClass) + "\"") + ","
                    + "\"input\":" + (inputPath == null ? "null"
                            : "\"" + escapeJson(inputPath) + "\"") + ","
                    + "\"mutator\":" + (mutatorClass == null ? "null"
                            : "\"" + escapeJson(mutatorClass) + "\"") + ","
                    + "\"query\":" + (queryClass == null ? "null"
                            : "\"" + escapeJson(queryClass) + "\"") + ","
                    + "\"query_result\":" + (queryResultJson == null ? "null"
                            : queryResultJson) + ","
                    + "\"solved\":" + solved + ","
                    + "\"study\":" + (solveStudy == null ? "null"
                            : "\"" + escapeJson(solveStudy) + "\"") + ","
                    + "\"mesh_tag\":" + (meshTag == null ? "null"
                            : "\"" + escapeJson(meshTag) + "\"") + ","
                    + "\"mesh_success\":" + (meshRan ? meshSuccess : "null")
                    + ","
                    + "\"elapsed_ms\":" + elapsedMs + "}");

        } catch (LicenseTimeoutException lte) {
            // A bounded license checkout expired — fail fast with a clear
            // reason rather than parking the JVM (and any partial seats)
            // indefinitely. System.exit in finally closes the TCP socket
            // so the floating-license server reclaims promptly.
            haltReason = "license_timeout";
            haltMessage = lte.getMessage();
            emitError(haltMessage);
        } catch (Throwable t) {
            if ("success".equals(haltReason)) {
                haltReason = "exception";
                haltMessage = t.getMessage() != null
                        ? t.getMessage() : t.getClass().getName();
            }
            emitException(t);
        } finally {
            // No partial save in no-save/query mode (nothing to persist),
            // and none without an output path to derive the partial name.
            if (model != null && !mainSaveDone && !noSave && outputPath != null) {
                attemptPartialSave(model, outputPath, haltReason);
            }
            SolverTelemetry.emit("halt", SolverTelemetry.payloadMixed(
                    "halt_reason", haltReason,
                    "message", haltMessage,
                    "error_detail", new SolverTelemetry.Raw(errorDetailJson)));
            haltEmitted = true;
            // Skip ModelUtil.disconnect() — it hangs in headless mode.
            // Process teardown via System.exit frees resources cleanly.
            System.exit("success".equals(haltReason) ? 0 : 1);
        }
    }

    // ---- Solver-stdout tee for solver_info capture --------------------------

    /**
     * PrintStream that passes every byte through to a wrapped original
     * stream AND additionally emits each complete non-TELEMETRY line as
     * a structured "solver_info" telemetry event.
     *
     * Installed on System.out and System.err only while study.run() is
     * executing, so banner output, compile logs, and our own non-solve
     * emissions are never captured.
     *
     * Design notes:
     * - TELEMETRY:-prefixed lines (emitted by SolverTelemetry.emit) are
     *   passthrough-only; they are never re-emitted as solver_info. This
     *   prevents feedback loops when the heartbeat thread writes a
     *   TELEMETRY line while the tee is installed.
     * - Line accumulation uses a ThreadLocal ByteArrayOutputStream so
     *   concurrent writes from the solve thread and the heartbeat
     *   thread cannot contaminate each other's line parsing.
     * - The parent PrintStream constructor wires `original` as
     *   FilterOutputStream.out; internal text/char writers flow through
     *   our overridden write(byte[], int, int), which is the single
     *   funnel that catches println/print/format/write(bytes) alike.
     * - All write() paths are non-throwing (PrintStream swallows
     *   IOExceptions); we preserve that contract.
     */
    static class SolverStdoutTee extends java.io.PrintStream {
        private final java.io.PrintStream original;
        private final ThreadLocal<java.io.ByteArrayOutputStream> lineBuf =
                ThreadLocal.withInitial(java.io.ByteArrayOutputStream::new);

        SolverStdoutTee(java.io.PrintStream original) {
            super(original, true);
            this.original = original;
        }

        @Override
        public void write(int b) {
            original.write(b);
            processByte(b);
        }

        @Override
        public void write(byte[] buf, int off, int len) {
            original.write(buf, off, len);
            for (int i = 0; i < len; i++) {
                processByte(buf[off + i] & 0xFF);
            }
        }

        private void processByte(int b) {
            java.io.ByteArrayOutputStream buf = lineBuf.get();
            if (b == '\n') {
                emitLine(buf);
            } else if (b != '\r') {
                buf.write(b);
            }
        }

        private void emitLine(java.io.ByteArrayOutputStream buf) {
            if (buf.size() == 0) return;
            String line;
            try {
                line = buf.toString("UTF-8");
            } catch (java.io.UnsupportedEncodingException e) {
                line = buf.toString();
            }
            buf.reset();
            if (line.isEmpty()) return;
            if (line.startsWith(SolverTelemetry.PREFIX)) return;
            try {
                SolverTelemetry.emit("solver_info",
                        SolverTelemetry.payload("message", line));
            } catch (Throwable ignored) {
                // Never let a telemetry hiccup corrupt solver output.
            }
        }

        /** Emit a trailing partial line (no newline) before uninstall. */
        void flushRemaining() {
            emitLine(lineBuf.get());
        }
    }

    // ---- Result extraction --------------------------------------------------

    /**
     * Walk user-defined probes and global numerical evaluations, emitting
     * one telemetry event per tag. Every call is hedged with a per-tag
     * try/catch so one failing probe cannot stop the rest, and the whole
     * method is hedged so extraction never breaks the save path.
     *
     * The COMSOL Java API shapes these as:
     *   model.probe().tags()               -> String[] of probe tag names
     *   model.probe(tag).getReal()         -> double[][] time history
     *   model.result().numerical().tags()  -> String[] of eval tag names
     *   model.result().numerical(tag).getReal() -> double[][] values
     * Any deviation in a specific COMSOL version falls through to a
     * result_error event with the Throwable's message.
     */
    private static void extractResults(Model model) {
        try {
            String[] probeTags = safeStringArray(
                    () -> model.probe().tags());
            for (String tag : probeTags) {
                try {
                    // Reflection: ProbeFeature.getReal() is not on every COMSOL
                    // version's typed interface (observed missing on one
                    // build, BM-05 c3 fanout). Reflect to keep this telemetry
                    // best-effort across versions; the per-tag try/catch below
                    // already swallows missing-method failures.
                    Object probe = model.probe(tag);
                    double[][] data = (double[][]) probe.getClass()
                            .getMethod("getReal").invoke(probe);
                    String series = formatSeries(data);
                    String unit = safeString(() -> model.probe(tag)
                            .getString("unit"));
                    SolverTelemetry.emit("result_probe",
                        "{\"tag\":\"" + escapeJson(tag) + "\","
                      + "\"name\":\"" + escapeJson(tag) + "\","
                      + "\"unit\":\"" + escapeJson(unit) + "\","
                      + "\"shape\":" + seriesShape(data) + ","
                      + "\"series\":" + series + "}");
                } catch (Throwable t) {
                    emitResultError("probe", tag, t);
                }
            }
        } catch (Throwable t) {
            emitResultError("probe_walk", "", t);
        }

        try {
            String[] numTags = safeStringArray(
                    () -> model.result().numerical().tags());
            for (String tag : numTags) {
                try {
                    double[][] data = model.result().numerical(tag).getReal();
                    String series = formatSeries(data);
                    String unit = safeString(() -> model.result()
                            .numerical(tag).getString("unit"));
                    SolverTelemetry.emit("result_global",
                        "{\"tag\":\"" + escapeJson(tag) + "\","
                      + "\"name\":\"" + escapeJson(tag) + "\","
                      + "\"unit\":\"" + escapeJson(unit) + "\","
                      + "\"shape\":" + seriesShape(data) + ","
                      + "\"series\":" + series + "}");
                } catch (Throwable t) {
                    emitResultError("global", tag, t);
                }
            }
        } catch (Throwable t) {
            emitResultError("numerical_walk", "", t);
        }
    }

    /** Returns "" if the supplier throws or yields null. Mirrors
     *  safeStringArray for scalar-string COMSOL property reads (unit,
     *  descr). Keeps per-tag extraction resilient to API shape drift:
     *  a probe that lacks a "unit" key just produces an empty unit
     *  string rather than aborting result emission. */
    private static String safeString(Callable<String> supplier) {
        try {
            String s = supplier.call();
            return s == null ? "" : s;
        } catch (Throwable t) {
            return "";
        }
    }

    /** Returns an empty String[] if the supplier throws. */
    private static String[] safeStringArray(Callable<String[]> supplier) {
        try {
            String[] a = supplier.call();
            return a == null ? new String[0] : a;
        } catch (Throwable t) {
            return new String[0];
        }
    }

    private static void emitResultError(String kind, String tag, Throwable t) {
        Throwable cause = t.getCause() != null ? t.getCause() : t;
        String msg = cause.getMessage() != null
                ? cause.getMessage() : cause.getClass().getName();
        SolverTelemetry.emit("result_error",
                SolverTelemetry.payload(
                        "kind", kind,
                        "tag", tag == null ? "" : tag,
                        "error", msg));
    }

    /**
     * JSON-serialize a double[][] as a nested array, capped at
     * RESULT_SERIES_MAX_ROWS rows. NaN/Infinity rendered as JSON null
     * since JSON has no native representation. A null input yields [].
     */
    private static String formatSeries(double[][] data) {
        if (data == null) return "[]";
        int rows = Math.min(data.length, RESULT_SERIES_MAX_ROWS);
        StringBuilder sb = new StringBuilder(rows * 16 + 4);
        sb.append("[");
        for (int i = 0; i < rows; i++) {
            if (i > 0) sb.append(",");
            double[] row = data[i];
            sb.append("[");
            if (row != null) {
                for (int j = 0; j < row.length; j++) {
                    if (j > 0) sb.append(",");
                    double v = row[j];
                    if (Double.isNaN(v) || Double.isInfinite(v)) {
                        sb.append("null");
                    } else {
                        sb.append(Double.toString(v));
                    }
                }
            }
            sb.append("]");
        }
        sb.append("]");
        return sb.toString();
    }

    /** [rows, cols] shape pair; cols is the width of the first non-null row
     *  or 0. Rows is the real count, not the capped emission count. */
    private static String seriesShape(double[][] data) {
        if (data == null) return "[0,0]";
        int rows = data.length;
        int cols = 0;
        for (double[] row : data) {
            if (row != null) { cols = row.length; break; }
        }
        return "[" + rows + "," + cols + "]";
    }

    // ---- Partial-save helper ------------------------------------------------

    /**
     * Derive the partial path for a given primary .mph output. If output
     * ends in ".mph", insert ".partial" before the extension; otherwise
     * append ".partial.mph".
     */
    static String partialPathFor(String outputPath) {
        if (outputPath == null) return null;
        if (outputPath.endsWith(".mph")) {
            return outputPath.substring(0, outputPath.length() - 4)
                    + ".partial.mph";
        }
        return outputPath + ".partial.mph";
    }

    private static void attemptPartialSave(
            final Model model, String outputPath, String reason) {
        final String partial = partialPathFor(outputPath);
        ExecutorService exec = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "partial-save");
            t.setDaemon(true);
            return t;
        });
        Future<Void> future = exec.submit((Callable<Void>) () -> {
            model.save(partial);
            return null;
        });
        try {
            future.get(PARTIAL_SAVE_TIMEOUT_MS, TimeUnit.MILLISECONDS);
            SolverTelemetry.emit("partial_save", SolverTelemetry.payload(
                    "path", partial, "reason", reason));
        } catch (TimeoutException te) {
            future.cancel(true);
            SolverTelemetry.emit("partial_save_timeout",
                    SolverTelemetry.payload(
                            "path", partial,
                            "reason", reason,
                            "timeout_ms",
                            Long.toString(PARTIAL_SAVE_TIMEOUT_MS)));
        } catch (Throwable t) {
            Throwable cause = (t.getCause() != null) ? t.getCause() : t;
            String msg = cause.getMessage() != null
                    ? cause.getMessage() : cause.getClass().getName();
            SolverTelemetry.emit("partial_save_failed",
                    SolverTelemetry.payload(
                            "path", partial,
                            "reason", reason,
                            "error", msg));
        } finally {
            exec.shutdownNow();
        }
    }

    // ---- Output helpers ----

    /** Raised when a bounded license-checkout operation exceeds its budget. */
    static final class LicenseTimeoutException extends RuntimeException {
        LicenseTimeoutException(String message) { super(message); }
    }

    /**
     * Run a callable with an optional wall-clock bound. When
     * {@code timeoutMs <= 0} the task runs inline (legacy behavior, waits
     * indefinitely). Otherwise it runs on a daemon thread and, if it does
     * not finish in time, a {@link LicenseTimeoutException} is thrown and
     * the worker is abandoned (its native checkout dies with the process
     * on the subsequent System.exit). The COMSOL license checkout
     * (initStandalone / load) is the motivating use: a seatless run
     * otherwise blocks forever holding partial checkouts. Topic-agnostic.
     */
    private static <T> T runBounded(
            Callable<T> task, long timeoutMs, String label) throws Exception {
        if (timeoutMs <= 0) {
            return task.call();
        }
        ExecutorService exec = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "license-guard");
            t.setDaemon(true);
            return t;
        });
        try {
            Future<T> future = exec.submit(task);
            try {
                return future.get(timeoutMs, TimeUnit.MILLISECONDS);
            } catch (TimeoutException te) {
                future.cancel(true);
                throw new LicenseTimeoutException(
                        label + " exceeded " + timeoutMs
                        + "ms — no license seat available?");
            } catch (java.util.concurrent.ExecutionException ee) {
                Throwable c = ee.getCause() != null ? ee.getCause() : ee;
                if (c instanceof Exception) throw (Exception) c;
                if (c instanceof Error) throw (Error) c;
                throw new RuntimeException(c);
            }
        } finally {
            exec.shutdownNow();
        }
    }

    private static void emit(String json) {
        System.out.println(json);
        System.out.flush();
    }

    private static void emitError(String message) {
        emit("{\"success\":false,"
            + "\"error\":\"" + escapeJson(message) + "\","
            + "\"error_detail\":" + errorDetailJson + "}");
    }

    /**
     * Reflectively harvest COMSOL's detailed diagnostic lines from a
     * Throwable and its cause chain. The real root cause of a failed
     * solve/load — the failing feature, the undefined variable, the
     * empty selection, the NaN DOF — is exposed by COMSOL's FlException
     * via {@code getTranslatableMessageArray()} and {@code getMessages()}
     * (both {@code String[]}-returning, and neither declared on
     * {@code java.lang.Throwable}), whereas {@code getMessage()} is
     * generic ("The following feature has encountered a problem").
     *
     * We invoke those methods reflectively so the toolkit never has to
     * compile against — or pin a version of — the COMSOL exception types,
     * and so a version that renames/drops them simply yields less detail
     * rather than a crash. Purely exception introspection: no model,
     * physics, or study-type knowledge, so it is fully topic-agnostic.
     *
     * Returns a de-duplicated, order-preserving list of non-empty lines,
     * with the generic {@code getMessage()} of each link appended as a
     * fallback. Best-effort: every reflective step is swallowed.
     *
     * Canonical implementation lives in {@link SolverTelemetry#errorDetail}
     * so mutators that swallow exceptions can harvest the
     * same detail via {@code SolverTelemetry.emitError(...)}; this method
     * delegates to keep a single source of truth.
     */
    static java.util.List<String> errorDetail(Throwable t) {
        return SolverTelemetry.errorDetail(t);
    }

    // ---- First-class mesh build (--mesh <tag>) ------------------

    static final int MESH_OK = 0;
    static final int MESH_BUILD_FAILED = 1;
    static final int MESH_TAG_NOT_FOUND = 2;

    /**
     * Run a mesh sequence with full instrumentation. Returns MESH_OK,
     * MESH_BUILD_FAILED (exception swallowed — the partial mesh commits
     * and the caller proceeds to save, per G-MESH-THROW-COMMITS-PARTIAL),
     * or MESH_TAG_NOT_FOUND (nothing ran — caller should abort).
     *
     * Emits: mesh_start, mesh_heartbeat (2 s cadence with rss/heap),
     * mesh_error (on a swallowed build exception, with full error_detail),
     * mesh_done (status success|error), mesh_census (always — score the
     * artifact, not the exception), and one mesh_feature_build per
     * feature (best-effort buildinfo/buildoutput/builddetails harvest —
     * these survive the aggregate throw, G-MESH-BUILDINFO-SURVIVES-THROW).
     */
    private static int runMeshSequence(Model model, String meshTag) {
        // Locate the owning component: mesh is component-scoped
        // (G-COMP-METHOD-NEEDS-COMPONENT).
        String compTag = null;
        for (String ct : model.component().tags()) {
            for (String mt : model.component(ct).mesh().tags()) {
                if (mt.equals(meshTag)) { compTag = ct; break; }
            }
            if (compTag != null) break;
        }
        if (compTag == null) return MESH_TAG_NOT_FOUND;
        final MeshSequence ms = model.component(compTag).mesh(meshTag);

        SolverTelemetry.emit("mesh_start", SolverTelemetry.payload(
                "mesh_tag", meshTag, "component", compTag));
        final long meshStart = System.currentTimeMillis();
        final String tagForBeat = meshTag;

        int rc = MESH_OK;
        ScheduledExecutorService heart = null;
        ScheduledFuture<?> beat = null;
        try {
            heart = Executors.newSingleThreadScheduledExecutor(
                new ThreadFactory() {
                    @Override
                    public Thread newThread(Runnable r) {
                        Thread t = new Thread(r, "mesh-heartbeat");
                        t.setDaemon(true);
                        return t;
                    }
                });
            beat = heart.scheduleAtFixedRate(() -> {
                try {
                    Runtime rt = Runtime.getRuntime();
                    SolverTelemetry.emit("mesh_heartbeat",
                        SolverTelemetry.payloadMixed(
                            "mesh_tag", tagForBeat,
                            "elapsed_mesh_ms",
                            System.currentTimeMillis() - meshStart,
                            "rss_bytes", SolverTelemetry.processRssBytes(),
                            "jvm_heap_used_bytes",
                            rt.totalMemory() - rt.freeMemory(),
                            "jvm_heap_max_bytes", rt.maxMemory()));
                } catch (Throwable ignored) {
                    // Never let telemetry break the mesh build.
                }
            }, HEARTBEAT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS,
               TimeUnit.MILLISECONDS);

            try {
                ms.run();   // whole-sequence: the only reliable commit point
                SolverTelemetry.emit("mesh_done", SolverTelemetry.payloadMixed(
                        "mesh_tag", meshTag, "status", "success",
                        "elapsed_mesh_ms",
                        System.currentTimeMillis() - meshStart));
            } catch (Exception e) {
                rc = MESH_BUILD_FAILED;
                // The throw is cosmetic; the partial mesh commits. Report
                // loudly, keep going, and let the census say what exists.
                SolverTelemetry.emitError("mesh_error", e);
                SolverTelemetry.emit("mesh_done", SolverTelemetry.payloadMixed(
                        "mesh_tag", meshTag, "status", "error",
                        "elapsed_mesh_ms",
                        System.currentTimeMillis() - meshStart));
            }
        } finally {
            if (beat != null) beat.cancel(false);
            if (heart != null) heart.shutdownNow();
        }

        emitMeshCensus(model, compTag, ms, meshTag);
        emitMeshFeatureBuilds(ms);
        return rc;
    }

    /**
     * Post-run element census: per-volume-type element counts and
     * distinct meshed domains via the single-arg
     * {@code getElemEntity(String)} (the census API that survived the
     * meshing-campaign audit), plus the geometry's domain count so the
     * unmeshed count is explicit. Reflection with per-step degradation:
     * a missing method yields -1 fields, never a crash.
     */
    private static void emitMeshCensus(Model model, String compTag,
                                       MeshSequence ms, String meshTag) {
        try {
            String[] volTypes = {"tet", "pyr", "prism", "hex"};
            StringBuilder counts = new StringBuilder("{");
            java.util.TreeSet<Integer> meshedDoms = new java.util.TreeSet<>();
            long totalVol = 0;
            boolean censusOk = false;
            for (int i = 0; i < volTypes.length; i++) {
                long n = 0;
                try {
                    Method m = ms.getClass().getMethod(
                            "getElemEntity", String.class);
                    Object r = m.invoke(ms, volTypes[i]);
                    if (r instanceof int[]) {
                        int[] doms = (int[]) r;
                        n = doms.length;
                        for (int d : doms) meshedDoms.add(d);
                        censusOk = true;
                    }
                } catch (Throwable ignored) {
                    n = -1;  // type absent or API unavailable
                }
                if (i > 0) counts.append(",");
                counts.append("\"").append(volTypes[i]).append("\":")
                      .append(n);
                totalVol += Math.max(0, n);
            }
            counts.append("}");

            // Domain universe (1-based ids — G-GETADJ-ROW-ZERO-EXTERIOR):
            // first geometry of the owning component, getNDomains().
            long nDomains = -1;
            try {
                String[] geomTags = model.component(compTag).geom().tags();
                if (geomTags.length > 0) {
                    Object geom = model.component(compTag).geom(geomTags[0]);
                    Method g = geom.getClass().getMethod("getNDomains");
                    Object nd = g.invoke(geom);
                    if (nd instanceof Number) {
                        nDomains = ((Number) nd).longValue();
                    }
                }
            } catch (Throwable ignored) {
                // Geometry introspection unavailable — census degrades.
            }
            long unmeshed = (censusOk && nDomains >= 0)
                    ? nDomains - meshedDoms.size() : -1;

            SolverTelemetry.emit("mesh_census", SolverTelemetry.payloadMixed(
                    "mesh_tag", meshTag,
                    "element_counts_by_type",
                    new SolverTelemetry.Raw(counts.toString()),
                    "total_volumetric_elements", censusOk ? totalVol : -1,
                    "meshed_domain_count",
                    censusOk ? (long) meshedDoms.size() : -1,
                    "n_domains", nDomains,
                    "unmeshed_domain_count", unmeshed));
        } catch (Throwable ignored) {
            // The census must never take down the run.
        }
    }

    /**
     * Per-feature build records: buildinfo/buildoutput/builddetails
     * survive the aggregate throw and are the real per-feature failure
     * census. Values are read best-effort (getString then
     * getStringArray), truncated to bound sidecar size.
     */
    private static void emitMeshFeatureBuilds(MeshSequence ms) {
        try {
            for (String ftag : ms.feature().tags()) {
                Object f;
                try {
                    f = ms.feature(ftag);
                } catch (Throwable ignored) {
                    continue;
                }
                StringBuilder payload = new StringBuilder("{");
                payload.append("\"feature\":\"")
                       .append(SolverTelemetry.escapeJson(ftag)).append("\"");
                for (String prop : new String[] {
                        "buildinfo", "buildoutput", "builddetails"}) {
                    String val = readStringProperty(f, prop);
                    if (val != null) {
                        if (val.length() > 2000) {
                            val = val.substring(0, 2000) + "…[truncated]";
                        }
                        payload.append(",\"").append(prop).append("\":\"")
                               .append(SolverTelemetry.escapeJson(val))
                               .append("\"");
                    }
                }
                payload.append("}");
                SolverTelemetry.emit("mesh_feature_build",
                        payload.toString());
            }
        } catch (Throwable ignored) {
            // Harvest must never take down the run.
        }
    }

    /** Best-effort string read of a feature property: getString, then
     *  getStringArray joined with newlines. Null when unreadable. */
    private static String readStringProperty(Object f, String prop) {
        try {
            Method m = f.getClass().getMethod("getString", String.class);
            Object r = m.invoke(f, prop);
            if (r instanceof String && !((String) r).isEmpty()) {
                return (String) r;
            }
        } catch (Throwable ignored) { }
        try {
            Method m = f.getClass().getMethod("getStringArray", String.class);
            Object r = m.invoke(f, prop);
            if (r instanceof String[]) {
                StringBuilder sb = new StringBuilder();
                for (String s : (String[]) r) {
                    if (s == null || s.isEmpty()) continue;
                    if (sb.length() > 0) sb.append("\n");
                    sb.append(s);
                }
                return sb.length() > 0 ? sb.toString() : null;
            }
        } catch (Throwable ignored) { }
        return null;
    }

    /**
     * General-purpose JSON serializer for the value a {@code query(...)}
     * contract returns. Handles the JSON-mappable shapes a query is
     * likely to produce — Map (object), Collection/Iterable and arrays
     * (including primitive arrays, via reflection), the boxed primitives,
     * String, and null — and falls back to a quoted {@code toString()}
     * for anything else. Non-finite doubles/floats are quoted (JSON has
     * no literal for them). Recursive; topic-agnostic.
     */
    static String jsonOf(Object v) {
        if (v == null) return "null";
        if (v instanceof String) return "\"" + escapeJson((String) v) + "\"";
        if (v instanceof Boolean || v instanceof Integer || v instanceof Long
                || v instanceof Short || v instanceof Byte) {
            return v.toString();
        }
        if (v instanceof Double) {
            double d = (Double) v;
            return (Double.isNaN(d) || Double.isInfinite(d))
                    ? "\"" + escapeJson(Double.toString(d)) + "\""
                    : Double.toString(d);
        }
        if (v instanceof Float) {
            float f = (Float) v;
            return (Float.isNaN(f) || Float.isInfinite(f))
                    ? "\"" + escapeJson(Float.toString(f)) + "\""
                    : Float.toString(f);
        }
        if (v instanceof Number) return v.toString();
        if (v instanceof Map) {
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) sb.append(",");
                first = false;
                sb.append("\"")
                  .append(escapeJson(String.valueOf(e.getKey())))
                  .append("\":").append(jsonOf(e.getValue()));
            }
            return sb.append("}").toString();
        }
        if (v instanceof Iterable) {
            StringBuilder sb = new StringBuilder("[");
            boolean first = true;
            for (Object o : (Iterable<?>) v) {
                if (!first) sb.append(",");
                first = false;
                sb.append(jsonOf(o));
            }
            return sb.append("]").toString();
        }
        if (v.getClass().isArray()) {
            StringBuilder sb = new StringBuilder("[");
            int n = java.lang.reflect.Array.getLength(v);
            for (int i = 0; i < n; i++) {
                if (i > 0) sb.append(",");
                sb.append(jsonOf(java.lang.reflect.Array.get(v, i)));
            }
            return sb.append("]").toString();
        }
        return "\"" + escapeJson(v.toString()) + "\"";
    }

    /** Render a list of strings as a JSON array literal. */
    static String jsonStringArray(java.util.List<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(escapeJson(items.get(i))).append("\"");
        }
        sb.append("]");
        return sb.toString();
    }

    private static void emitException(Throwable e) {
        Throwable cause = (e.getCause() != null) ? e.getCause() : e;
        StringBuilder trace = new StringBuilder();
        for (StackTraceElement el : cause.getStackTrace()) {
            if (trace.length() > 0) trace.append("\\n");
            trace.append(el.toString());
        }
        String msg = cause.getMessage() != null
            ? cause.getMessage()
            : cause.getClass().getName();
        // Harvest detail from the *original* throwable so an FlException
        // sitting above the unwrapped cause is still inspected. Stash it
        // for the terminal `halt` event too.
        errorDetailJson = jsonStringArray(errorDetail(e));
        emit("{\"success\":false,"
            + "\"error\":\"" + escapeJson(msg) + "\","
            + "\"error_detail\":" + errorDetailJson + ","
            + "\"stack_trace\":\"" + escapeJson(trace.toString()) + "\"}");
    }

    /** JSON string escape (kept in sync with SolverTelemetry.escapeJson).
     *  Control characters below 0x20 must be \\u-escaped or the final
     *  envelope line — the one the Python side must parse — is invalid
     *  JSON and the whole run is misreported as "no JSON envelope". */
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
