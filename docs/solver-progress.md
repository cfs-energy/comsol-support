# Solver progress instrumentation

## The constraint

**COMSOL 6.4's public Java API does not expose a per-step solver
callback.** There is no hook on `Study`, `SolverSequence`, or any
solver feature that fires once per Newton step, per time step, per
parametric step, or per matrix factorization. Multiple cycles of
reflective probing against `com.comsol.model.*` across the 6.4 release
line confirmed this — `study.run()` is a single blocking call from the
caller's perspective.

This is a confirmed-negative dead end. The expected COMSOL extension
point (a `ProgressListener`-style callback) simply does not exist in
the public API surface. Vendor escalation would be required to add it.

Future campaigns that hit "I'd like to know what the solver is doing
right now" should not spend cycles re-probing the API. Apply one of the
two canonical workarounds below.

## Canonical workaround A — polling heartbeat (recommended)

Spawn one daemon thread that emits a "still alive" telemetry event at
a fixed cadence while `study.run()` is in flight, on the main solve
thread. The heartbeat doesn't tell you *what* the solver is doing — it
tells you *that* it's still doing something, which is enough to
distinguish a long solve from a hung solve and to give downstream
consumers (the user, an MCP search tool, a watchdog) liveness signal.

Reference implementation: [`SolverHeartbeat.java`](../comsol_support/java/SolverHeartbeat.java).

```java
import com.comsol.model.*;
import com.comsol.model.util.*;

import java.util.HashMap;
import java.util.Map;

public class MyCustomHarness {

    public static void main(String[] argv) {
        ModelUtil.initStandalone(false);
        Model model = ModelUtil.load("loaded", argv[0]);

        long t0 = System.currentTimeMillis();
        SolverHeartbeat hb = SolverHeartbeat.start(
            () -> SolverTelemetry.emit(
                "solver_heartbeat",
                SolverTelemetry.payloadMixed(
                    "study_tag", "std1",
                    "elapsed_solve_ms", System.currentTimeMillis() - t0)),
            2_000L);
        try {
            model.study("std1").run();
        } finally {
            hb.stop();
        }
        model.save(argv[1]);
        System.exit(0);
    }
}
```

The `mphgen` / `edit-mph` `ModelExporter` does this automatically — you
only need `SolverHeartbeat` directly when authoring a bespoke harness.

**Cadence choice.** The default is 2 s. Reasoning:
- < 1 s: bloats the telemetry sink on multi-hour solves with no
  added signal (the solve isn't going to recover by the next beat).
- 1–5 s: sweet spot. Confirms liveness on the timescale a human or
  watchdog notices.
- > 10 s: indistinguishable from "hung" to a human watcher.

## Canonical workaround B — JVM thread-dump polling (when A is insufficient)

When the heartbeat alone is not enough (e.g. you need to know *which*
solver phase is running, or whether the solve is stuck on a specific
operation), a deeper pattern is to periodically dump the JVM's solver
thread stack and emit the top N frames as a telemetry event. This costs
~10 ms per dump and produces useful diagnostic detail on hangs.

This pattern is heavier than A and not yet promoted to a library — if
your campaign needs it, the thread-dump pattern that has been used
in-the-wild is:

```java
ThreadMXBean bean = ManagementFactory.getThreadMXBean();
ThreadInfo[] threads = bean.dumpAllThreads(false, false);
for (ThreadInfo ti : threads) {
    if (ti.getThreadName().toLowerCase().contains("solver")
            || ti.getThreadName().toLowerCase().contains("comsol")) {
        StringBuilder frames = new StringBuilder();
        for (StackTraceElement el : ti.getStackTrace()) {
            frames.append(el.toString()).append("\n");
            if (frames.length() > 4_000) break;  // cap
        }
        SolverTelemetry.emit("solver_stack",
            SolverTelemetry.payload(
                "thread", ti.getThreadName(),
                "state", ti.getThreadState().toString(),
                "frames", frames.toString()));
    }
}
```

When this pattern reaches its third campaign, lift it into a
`SolverStackDump.java` utility next to `SolverHeartbeat.java`.

## What does NOT work

The following were attempted across multiple campaigns and confirmed
non-functional:

| Attempt | Outcome |
|---|---|
| `model.study(tag).feature(solverTag).setStudyMonitor(...)` | No public method by this name (or any name like it). |
| Reflecting on `SolverSequence` for listener-shaped methods | Yields setup methods only (no callback registration). |
| Subscribing to `ModelUtil` log handlers | Receives banner / warning text, not per-step progress. |
| `Future`-style `study.run()` | `run()` is `void`; no async variant exists. |
| Vendor stubs in `com.comsol.api.*` | Only generic JNI/process classes — none expose solver callbacks. |

If a new probe targets one of these and reveals different behavior in a
future COMSOL version, append the finding here.

## Integration with `SolverTelemetry`

The heartbeat is just one event type in the structured telemetry
stream emitted by `SolverTelemetry`. Other events relevant during a
solve:

| Event | When | Notes |
|---|---|---|
| `solve_start` | Just before `study.run()` | Emitted by ModelExporter |
| `solve_done` | After `study.run()` returns | success or error status |
| `solver_heartbeat` | Every 2 s during solve | from `SolverHeartbeat` |
| `solver_info` | Each non-TELEMETRY stdout line during solve | tee captures COMSOL's own log output |
| `partial_save` | On any failure path before main save | preserves diagnostic state |
| `result_probe`, `result_global` | Post-solve | extracted numerical outputs |

Consumers (mphgen sidecar, `comsol-support ingest-telemetry`, the MCP
`get_solver_telemetry` tool) handle all events uniformly. There's no
special-case path for the heartbeat — it's just another structured
event in the stream.

## Testing

```bash
# SolverHeartbeat compile + smoke (no COMSOL runtime needed):
uv run pytest tests/test_lint_selftest.py::test_all_java_sources_compile
uv run pytest tests/test_solver_heartbeat.py
```
