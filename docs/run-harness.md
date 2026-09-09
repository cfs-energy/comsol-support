# run-harness — run any COMSOL-dependent Java class

`run-harness` is the generic standalone-harness runner. Where `mphgen`
and `edit-mph` are purpose-built, `run-harness` compiles (if needed) and
runs **any** class against the assembled COMSOL classpath + native
library environment, and tees the complete raw output to a log. It
exists to collapse the throwaway "compile + classpath + env + Popen +
filter stdout" scripts that every ad-hoc diagnostic otherwise re-creates.

## CLI

```bash
comsol-support run-harness MyProbe.java arg1 arg2 \
    --timeout 120 \
    --log /tmp/myprobe.log
```

- `MyProbe.java` — a `.java` source (compiled on demand to the
  workspace) **or** a bare class name already on the workspace classpath.
  The class needs a standard `public static void main(String[])`.
- positional `arg1 arg2 …` — forwarded to `main`.
- `--log PATH` — full-output log (default
  `<workspace>/<class>.harness.log`). The log always captures the
  **complete, unfiltered** stdout+stderr, so the COMSOL solver log and
  any `SIGQUIT` thread dump survive even when the console view is
  filtered. if the log ends up EMPTY, the CLI prints a
  loud warning and the result JSON carries `log_bytes: 0` — an empty
  log means the process emitted nothing before it ended; never cite
  absence of output as a measurement.
- `--timeout SECONDS` — terminate the JVM's process group on expiry.
- `--jvm-arg FLAG` — extra JVM flag (repeatable; replaces the default
  headless + `-Xmx48g` set when given).
- `--quiet` — don't mirror output live to stderr (the log still gets it).

Prints `{"success", "returncode", "log"}`; exit code mirrors the class's.

## Python API

```python
from comsol_support.java_facade import JavaFacade
facade = JavaFacade(comsol_path, workspace_dir)
rc, out = facade.run_class(
    "MyProbe.java", ["arg1"],
    stream=True, log_path="/tmp/myprobe.log", timeout=120,
)
```

`run_class` is the single launch primitive now shared by the toolkit: it
assembles the classpath/env, launches the JVM in **its own process
group** (`start_new_session`), tees the full stream to the log while
optionally mirroring a filtered console view, and on timeout terminates
the whole group — SIGTERM first (clean socket close → prompt license
release), escalating to SIGKILL only after a grace period.

## Getting a thread dump from a hung solve

The COMSOL-bundled JRE ships **no `jstack`/`jcmd`**. Use `SIGQUIT`
instead: `kill -3 <pid>` makes the JVM print a full thread dump to its
stdout, which `run-harness` tees to the log. Find the PID with
`pgrep -f <ClassName>`. See gotcha `G-NO-JSTACK-USE-SIGQUIT`.

---

# license-status — is a COMSOL seat available?

The bundled `lmutil`/`lmstat` can't query every FlexNet server (version
mismatch — `-12`/`-96` measured), so `license-status` instead *attempts* a
base-license checkout under a wall-clock bound:

```bash
comsol-support license-status --timeout 30
```

- `ModelUtil.initStandalone`, then `ModelUtil.checkoutLicense("COMSOL")`
  returns true in time → a seat was free:
  `{"available": true, "checkout_ms": <n>}` (exit 0).
- The checkout returns false → every base seat is in use:
  `{"available": false, "reason": "no_seat", "error": …}` (exit 2).
- The probe blocks past `--timeout` → the process group is killed
  (clean release) and `{"available": false, "reason": "timeout"}` is
  reported (exit 2).

The explicit `checkoutLicense` step is the whole point: measured on
6.4.0.293 against a floating pool with every seat taken,
`initStandalone` still returned in ~2 s (it needs no seat — the base
"COMSOL Multiphysics" seat is only taken at `ModelUtil.load`), so a probe
that stopped there reported `available: true` while every model load
failed with `Could_not_obtain_license_for#COMSOL Multiphysics`.
`ModelUtil.checkoutLicenseForFile()` is *not* a substitute: it is an
entitlement check ("the license contains the products this file needs")
and stays true during seat exhaustion.

## License-checkout timeout for normal runs

The same bound is available on `mphgen`, `edit-mph`, and `query-mph` via
`--license-timeout SECONDS` (or the `COMSOL_LICENSE_TIMEOUT` environment
variable). When set, a run with no free seat **fails fast** as
`halt_reason=license_timeout` instead of parking the JVM indefinitely
while holding partial checkouts. Omitted / non-positive = wait
indefinitely (legacy behavior). Because every JVM launch now owns its
process group and is torn down with a clean group SIGTERM, a killed or
timed-out run releases its seat promptly rather than leaving a ghost
checkout on the server.
