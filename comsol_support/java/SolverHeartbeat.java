/**
 * SolverHeartbeat — periodic "still alive" emitter for long-running
 * COMSOL solves.
 *
 * COMSOL 6.4's public Java API does not expose a per-step solver
 * callback. There is no `study.run()` hook that fires per Newton step,
 * per time step, or per parametric step. This was verified across many
 * cycles of reflective probing against `com.comsol.model.*` (see
 * docs/solver-progress.md). The canonical workaround is to spawn a
 * single daemon thread that emits a structured "still alive" telemetry
 * event at a fixed cadence while `study.run()` is in flight, on the
 * main solve thread.
 *
 * Used by ModelExporter.java for mphgen / edit-mph runs. Surfaced as
 * its own class so any campaign-authored harness can use the same
 * pattern without re-deriving it.
 *
 * Usage:
 *
 *   long t0 = System.currentTimeMillis();
 *   SolverHeartbeat hb = SolverHeartbeat.start(
 *       () -> SolverTelemetry.emit(
 *           "solver_heartbeat",
 *           SolverTelemetry.payloadMixed(
 *               "study_tag", "std1",
 *               "elapsed_solve_ms", System.currentTimeMillis() - t0)),
 *       2_000L);
 *   try {
 *       model.study("std1").run();
 *   } finally {
 *       hb.stop();
 *   }
 *
 * Design notes:
 * - Single-threaded ScheduledExecutorService — beats are serial; a
 *   slow beat callback delays the next beat rather than overlapping.
 * - Daemon thread — JVM exits even if .stop() is missed.
 * - Beat callback exceptions are swallowed — a telemetry hiccup must
 *   never propagate into the solve thread or be a reason for a beat
 *   to silently stop firing.
 * - Cancellation is "best effort"; the executor is shutdownNow() to
 *   interrupt any in-flight beat, but a beat that has already entered
 *   the user-supplied Runnable will complete (interrupting is the
 *   callback's responsibility if it does anything blocking).
 */

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;

public final class SolverHeartbeat {

    /** Recommended default cadence. Dense enough to confirm liveness
     *  on the order of "the solve isn't dead" without bloating any
     *  downstream telemetry sink on multi-hour solves. */
    public static final long DEFAULT_INTERVAL_MS = 2_000L;

    private final ScheduledExecutorService scheduler;
    private final ScheduledFuture<?> beat;
    private volatile boolean stopped;

    private SolverHeartbeat(ScheduledExecutorService scheduler,
                            ScheduledFuture<?> beat) {
        this.scheduler = scheduler;
        this.beat = beat;
    }

    /**
     * Start a heartbeat that fires `onBeat` every `intervalMs`.
     *
     * @param onBeat   user-supplied callback (telemetry emit, log, etc.).
     *                 Exceptions thrown are swallowed.
     * @param intervalMs cadence in milliseconds. Must be > 0.
     * @return a SolverHeartbeat handle; call .stop() to cancel.
     */
    public static SolverHeartbeat start(Runnable onBeat, long intervalMs) {
        if (onBeat == null) {
            throw new IllegalArgumentException("onBeat is required");
        }
        if (intervalMs <= 0) {
            throw new IllegalArgumentException(
                "intervalMs must be > 0; got " + intervalMs);
        }
        ScheduledExecutorService scheduler =
            Executors.newSingleThreadScheduledExecutor(new ThreadFactory() {
                @Override
                public Thread newThread(Runnable r) {
                    Thread t = new Thread(r, "solver-heartbeat");
                    t.setDaemon(true);
                    return t;
                }
            });
        ScheduledFuture<?> beat = scheduler.scheduleAtFixedRate(() -> {
            try {
                onBeat.run();
            } catch (Throwable ignored) {
                // Never let a beat callback failure stop subsequent
                // beats or propagate into the solve thread.
            }
        }, intervalMs, intervalMs, TimeUnit.MILLISECONDS);
        return new SolverHeartbeat(scheduler, beat);
    }

    /** Start with the default cadence (2 s). */
    public static SolverHeartbeat start(Runnable onBeat) {
        return start(onBeat, DEFAULT_INTERVAL_MS);
    }

    /**
     * Stop firing. Idempotent — subsequent calls are no-ops. Returns
     * promptly; does not wait for an in-flight beat to complete (the
     * thread is daemon-flagged so JVM exit is unaffected).
     */
    public void stop() {
        if (stopped) return;
        stopped = true;
        try { beat.cancel(true); } catch (Throwable ignored) {}
        try { scheduler.shutdownNow(); } catch (Throwable ignored) {}
    }

    public boolean isStopped() { return stopped; }
}
