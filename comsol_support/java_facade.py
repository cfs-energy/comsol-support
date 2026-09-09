"""Java facade — Python driver for compiling and running the Java side
(TagRegistry, SelectionAlgebra, ModelExporter, probes, ad-hoc harnesses).

Manages classpath assembly, Java compilation, subprocess execution,
and JSON output parsing. All Java interaction goes through this module.

Subprocess calls are plain ``subprocess.run`` / ``Popen`` (mocked in tests).
"""

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


# SIGKILL does not exist in the Windows signal module; 9 is its
# universal POSIX number. It is only ever *sent* down the POSIX
# group-signal path — the Windows fallback uses proc.kill().
_SIGKILL = getattr(signal, "SIGKILL", 9)

# Fixed POSIX signal numbering for decoding -N subprocess returncodes.
# Kept static so forensics on telemetry recorded on Linux decode
# identically on every platform.
_POSIX_SIGNAMES = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP",
    6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1",
    11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM",
    15: "SIGTERM",
}


# ---- Exceptions ----

class JavaNotFoundError(Exception):
    """Neither system javac nor COMSOL-bundled JDK found."""


class JavaCompileError(Exception):
    """Java compilation failed."""


class JavaExecutionError(Exception):
    """Java process returned non-zero or unparseable output."""


# ---- Data classes ----

@dataclass
class CompileResult:
    success: bool
    stdout: str
    stderr: str
    command: list = field(default_factory=list)


# ---- Facade names ----

# Java source files that DON'T need COMSOL jars
FACADE_SOURCES = ["TagRegistry.java", "SelectionAlgebra.java"]

# Java source file that DOES need COMSOL jars


class JavaFacade:
    """Python wrapper for Java compilation and execution.

    Args:
        comsol_path: Root of the COMSOL installation (e.g., /usr/local/comsol62).
        workspace_dir: Build workspace directory for compiled classes and artifacts.
        java_source_dir: Override for Java source location (default: comsol_support/java/).
    """

    def __init__(
        self,
        comsol_path: str,
        workspace_dir: str | Path,
        java_source_dir: str | Path | None = None,
    ):
        self.comsol_path = Path(comsol_path)
        self.workspace_dir = Path(workspace_dir)
        self.java_source_dir = (
            Path(java_source_dir) if java_source_dir
            else Path(__file__).parent / "java"
        )
        default_dir = Path(__file__).parent / "java"
        if (self.java_source_dir.resolve() == default_dir.resolve()
                and not default_dir.is_dir()):
            # Installed from a wheel: the package has no java/ tree, so
            # every compile would fail downstream with a bare javac
            # usage error. Say so once, up front. Callers that pass their
            # own java_source_dir (tests, ad-hoc harnesses) are exempt.
            from comsol_support._source_checkout import require_source_checkout
            require_source_checkout("The Java facade (mphgen, edit-mph, "
                                    "query-mph, run-harness, license-status, "
                                    "check on .mph)",
                                    root=default_dir.parent.parent)
        self.compiled_dir = self.workspace_dir / "java_compiled"
        self._facade_compiled = False

    # ---- Java discovery ----

    def find_java_executable(self, tool: str = "javac") -> str:
        """Find javac or java executable.

        Search order:
        1. COMSOL's bundled JDK at {comsol_path}/java/{platform}/jre/
        2. System PATH (via shutil.which)
        3. JAVA_HOME/bin/

        The bundled JDK comes first because it is the only one
        guaranteed version-compatible with the COMSOL plugin JARs:
        6.4 ships class files for Java 21, while a system JDK is
        whatever the OS happens to carry (macOS's /usr/bin/javac shim
        resolved to JDK 11 here, which cannot even load the plugins).
        Installs without a bundled JDK fall through to PATH unchanged.

        Returns the path string. Raises JavaNotFoundError if not found.
        """
        # Windows executables carry .exe; probe both spellings so the
        # same code path serves POSIX and Windows layouts.
        tool_names = [f"{tool}.exe", tool] if os.name == "nt" else [tool]

        # 1. COMSOL bundled JDK (shipped under java/{plat}/jre even
        #    though it is a full JDK — javac is present on 6.4). On
        #    macOS the JDK is a .app-style bundle, so the binaries live
        #    under jre/Contents/Home/bin instead of jre/bin; probe both
        #    layouts unconditionally (the extra path simply doesn't
        #    exist elsewhere).
        plat = _platform_subdir(self.comsol_path)
        jdk_root = self.comsol_path / "java" / plat / "jre"
        jdk_bins = [jdk_root / "bin", jdk_root / "Contents" / "Home" / "bin"]
        for jdk_bin in jdk_bins:
            for name in tool_names:
                comsol_jdk = jdk_bin / name
                if comsol_jdk.exists():
                    return str(comsol_jdk)

        # 2. System PATH
        system_path = shutil.which(tool)
        if system_path:
            return system_path

        # 3. JAVA_HOME
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            for name in tool_names:
                jh_path = Path(java_home) / "bin" / name
                if jh_path.exists():
                    return str(jh_path)
        raise JavaNotFoundError(
            f"Cannot find '{tool}'. Checked: COMSOL JDK at "
            f"{jdk_bins[0] / tool_names[0]}, system PATH, JAVA_HOME."
        )

    def find_comsol_jars(self) -> list[Path]:
        """Find COMSOL JAR files in {comsol_path}/plugins/.

        Returns ALL JAR files — COMSOL's OSGi architecture requires the
        full plugin set for compilation and runtime (the public API in
        com.comsol.api_1.0.0.jar references types across many bundles).
        Returns an empty list if the plugins directory doesn't exist.
        """
        plugins_dir = self.comsol_path / "plugins"
        if not plugins_dir.is_dir():
            return []

        return sorted(p for p in plugins_dir.iterdir() if p.suffix == ".jar")

    def find_standalone_slf4j_binding(self) -> Path | None:
        """Locate a non-OSGi SLF4J binding shipped with COMSOL.

        Why this matters: COMSOL's `plugins/` directory ships
        `org.osgi.slf4j.osgi-*.jar` — an OSGi-specific SLF4J binding
        whose `StaticLoggerBinder.<clinit>` only resolves under an OSGi
        class-loader. In a flat-classpath standalone Java app (which is
        what every comsol-support entry point uses), that binding fails
        to initialize. The failure propagates up: any model that touches
        the Heat Transfer module's ASHRAE ambient-properties database
        (which loads via SLF4J → SQLite JDBC at class-init time) fails
        to load with the cryptic "Failed_to_initialize_physics_interface".

        Resolution: COMSOL also ships a plain JDK14-backed SLF4J binding
        at `bin/tomcat/lib/org.slf4j.slf4j-jdk14-*.jar` (intended for the
        bundled Tomcat web layer, but works perfectly as a standalone
        binding). Prepending it to the classpath ahead of the OSGi
        binding lets the JVM resolve `StaticLoggerBinder` to the plain
        binding, unblocking the load chain.

        Returns the .jar path if found, otherwise None (caller handles
        gracefully — COMSOL versions that don't ship this binding will
        just not get the fix).
        """
        candidates = self.comsol_path / "bin" / "tomcat" / "lib"
        if not candidates.is_dir():
            return None
        # Match slf4j-jdk14-*.jar (most stable) and fall back to any
        # non-OSGi binding. Filenames seen so far:
        #   bin/tomcat/lib/org.slf4j.slf4j-jdk14-1.7.36.jar
        for pat in ("org.slf4j.slf4j-jdk14-*.jar",
                    "slf4j-jdk14-*.jar",
                    "org.slf4j.slf4j-simple-*.jar",
                    "slf4j-simple-*.jar"):
            hits = sorted(candidates.glob(pat))
            if hits:
                return hits[0]
        return None

    # ---- Classpath ----

    def get_facade_classpath(self) -> str:
        """Classpath for compiling facade files (no COMSOL jars needed)."""
        return str(self.compiled_dir)

    def get_full_classpath(self) -> str:
        """Classpath for compiling/running COMSOL-dependent code.

        Order:
          1. The workspace's compiled_dir (for our own classes)
          2. A standalone-friendly SLF4J binding (see
             `find_standalone_slf4j_binding`) — must precede the
             plugins/ jars so it wins over the OSGi binding
          3. All COMSOL plugin JARs via a `plugins/*` classpath
             wildcard. Both java and javac expand it natively, entries
             are processed left-to-right so the SLF4J entry still wins,
             and one entry instead of 300+ absolute JAR paths keeps the
             command line under Windows' ~32k limit (explicit
             enumeration exceeded it: WinError 206).
        """
        parts = [str(self.compiled_dir)]
        slf4j = self.find_standalone_slf4j_binding()
        if slf4j is not None:
            parts.append(str(slf4j))
        plugins_dir = self.comsol_path / "plugins"
        if plugins_dir.is_dir():
            parts.append(str(plugins_dir / "*"))
        return os.pathsep.join(parts)

    # ---- Runtime environment ----

    def get_comsol_env(self) -> dict[str, str]:
        """Build environment dict with COMSOL native library paths.

        COMSOL's ModelUtil.initStandalone() loads native libraries (e.g.,
        libcssystemutil.so, libiomp5.so) from lib/{platform}/ and
        lib/{platform}/ext/. This method constructs LD_LIBRARY_PATH
        (DYLD_LIBRARY_PATH on macOS; PATH on Windows, where the loader
        resolves DLLs from PATH) so the JVM can find them.
        """
        env = os.environ.copy()
        plat = _platform_subdir(self.comsol_path)
        lib_dir = self.comsol_path / "lib" / plat
        ext_dir = lib_dir / "ext"

        lib_paths = []
        if lib_dir.is_dir():
            lib_paths.append(str(lib_dir))
        if ext_dir.is_dir():
            lib_paths.append(str(ext_dir))

        # COMSOL extension libraries (cadimport, graphicsmagick, etc.)
        # are loaded lazily — e.g., the Block geometry primitive pulls
        # ext/cadimport/glnxa64/libpskernel.so. Add every ext/*/glnxa64
        # subdir defensively; missing dirs are skipped.
        ext_root = self.comsol_path / "ext"
        if ext_root.is_dir():
            for sub in sorted(ext_root.iterdir()):
                sublib = sub / plat
                if sublib.is_dir():
                    lib_paths.append(str(sublib))

        if lib_paths:
            if plat == "win64":
                # Windows resolves native DLLs via PATH — prepend so
                # COMSOL's DLLs win over any same-named system copies.
                path_var = "PATH"
            elif plat.startswith("mac"):
                path_var = "DYLD_LIBRARY_PATH"
            else:
                path_var = "LD_LIBRARY_PATH"
            existing = env.get(path_var, "")
            env[path_var] = os.pathsep.join(lib_paths + ([existing] if existing else []))

        return env

    # ---- Compilation ----

    def compile_facade(self) -> CompileResult:
        """Compile TagRegistry.java and SelectionAlgebra.java.

        These don't need COMSOL jars — only java.util.* imports.
        Output goes to self.compiled_dir.
        """
        javac = self.find_java_executable("javac")
        self.compiled_dir.mkdir(parents=True, exist_ok=True)

        sources = [
            str(self.java_source_dir / src) for src in FACADE_SOURCES
        ]

        cmd = [javac, "-d", str(self.compiled_dir)] + sources

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )

        cr = CompileResult(
            success=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            command=cmd,
        )

        if cr.success:
            self._facade_compiled = True

        return cr


    def compile_comsol_class(self, source_name: str) -> CompileResult:
        """Compile one COMSOL-dependent class from java_source_dir by name.

        Entry points that shell out to `java <ClassName>` must call this
        first — nothing else in the pipeline compiles them, and a missing
        .class surfaces only as a ClassNotFoundException on stderr, which
        callers that parse stdout silently read as "zero work done".

        `-sourcepath` is what makes this safe to call for one class at a
        time: our sources reference each other (ModelChecker uses
        SlotHarvester, ModelExporter uses SolverTelemetry), and javac
        otherwise defaults the source path to the *class* path, where
        the .java files are not. Without it, any order that compiles a
        dependent first fails with "cannot find symbol".
        """
        return self._javac([str(self.java_source_dir / source_name)])

    def compile_comsol_sources(
        self, source_names: list[str] | None = None,
    ) -> CompileResult:
        """Compile every COMSOL-dependent Java source in one javac call.

        What the installers use. One invocation instead of eleven: javac
        resolves the inter-source dependencies itself, so no compile
        order can be wrong, and it is several times faster than looping.
        """
        if source_names is None:
            sources = sorted(self.java_source_dir.glob("*.java"))
        else:
            sources = [self.java_source_dir / n for n in source_names]
        if not sources:
            return CompileResult(success=True, stdout="", stderr="",
                                 command=[])
        return self._javac([str(p) for p in sources])

    def _javac(self, sources: list[str]) -> CompileResult:
        """Run javac over `sources` with the full COMSOL classpath."""
        javac = self.find_java_executable("javac")
        self.compiled_dir.mkdir(parents=True, exist_ok=True)

        cmd = [javac,
               "-cp", self.get_full_classpath(),
               "-sourcepath", str(self.java_source_dir),
               "-d", str(self.compiled_dir),
               *sources]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )

        return CompileResult(
            success=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            command=cmd,
        )

    def compile_stage_code(self, source_path: Path) -> CompileResult:
        """Compile a stage-generated .java file with full classpath."""
        javac = self.find_java_executable("javac")
        self.compiled_dir.mkdir(parents=True, exist_ok=True)

        cp = self.get_full_classpath()
        cmd = [javac, "-cp", cp, "-d", str(self.compiled_dir),
               str(source_path)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )

        return CompileResult(
            success=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            command=cmd,
        )

    # ---- Execution ----


    def run_class(
        self,
        java_source_or_class: str | Path,
        args: list | tuple = (),
        *,
        stream: bool = False,
        log_path: str | Path | None = None,
        timeout: int | None = None,
        console_filter=None,
        jvm_args: list | tuple = ("-Djava.awt.headless=true", "-Xmx48g"),
        compile_if_needed: bool = True,
        mirror=None,
    ) -> tuple[int, str]:
        """Compile (if needed) and run an arbitrary COMSOL-dependent class.

        It removes the ~identical "compile → full classpath → comsol env → Popen → filter
        stdout" boilerplate that every ad-hoc diagnostic harness otherwise
        re-implements. Model- and task-agnostic: it runs whatever class you
        give it.

        Args:
            java_source_or_class: A path to a `.java` file (compiled to the
                workspace if its `.class` is missing/stale) OR a bare class
                name already on the classpath.
            args: Arguments passed to the class's `main`.
            stream: If True, mirror the (optionally filtered) console view
                live to `mirror` (defaults to sys.stderr) as lines arrive.
            log_path: If set, tee the FULL raw stdout+stderr to this file —
                unfiltered, so the COMSOL solver log and any SIGQUIT thread
                dump are never lost even when `console_filter` hides them.
            timeout: Seconds before the process group is terminated.
            console_filter: Optional predicate `(line: str) -> bool`
                selecting which lines reach the console mirror. The log
                file always gets every line regardless.
            jvm_args: JVM flags (default: headless + 48g heap).
            compile_if_needed: Compile a `.java` source when its class file
                is absent or older than the source.
            mirror: File-like sink for the streamed console view
                (default sys.stderr when `stream`).

        Returns:
            (returncode, full_stdout) — full_stdout is the complete merged
            stream regardless of console filtering.

        Raises:
            JavaCompileError if a `.java` source fails to compile.
            JavaExecutionError on timeout.
        """
        src = Path(java_source_or_class)
        if src.suffix == ".java":
            class_name = _public_class_name(src)
            class_file = self.compiled_dir / f"{class_name}.class"
            stale = (
                not class_file.exists()
                or src.stat().st_mtime > class_file.stat().st_mtime
            )
            if compile_if_needed and stale:
                cr = self.compile_stage_code(src)
                if not cr.success:
                    raise JavaCompileError(
                        f"Failed to compile {src.name}:\n{cr.stderr}"
                    )
        else:
            class_name = str(java_source_or_class)

        java = self.find_java_executable("java")
        cp = self.get_full_classpath()
        cmd = [java, *jvm_args, "-cp", cp, class_name,
               *[str(a) for a in args]]

        if mirror is None and stream:
            mirror = sys.stderr

        # One-COMSOL-JVM discipline — see jvm_slot.py.
        from comsol_support.jvm_slot import acquire_jvm_slot, warn_leaked_jvms
        warn_leaked_jvms()
        jvm_slot = acquire_jvm_slot(f"run_class {class_name}")

        log_fh = (open(log_path, "w", encoding="utf-8")
                  if log_path is not None else None)
        captured: list[str] = []
        drain_state: dict = {"error": None}

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            # Harness/solver output may carry non-UTF8 bytes; never let a
            # stray byte abort the drain thread. Pin UTF-8 so Windows
            # doesn't decode with the legacy locale codepage.
            encoding="utf-8", errors="replace",
            bufsize=1, env=self.get_comsol_env(),
            start_new_session=(os.name == "posix"),
        )

        def _drain():
            try:
                for line in proc.stdout:
                    captured.append(line)
                    if log_fh is not None:
                        log_fh.write(line)
                        log_fh.flush()
                    if mirror is not None and (
                        console_filter is None or console_filter(line)
                    ):
                        mirror.write(line)
                        mirror.flush()
            except BaseException as e:  # noqa: BLE001 — record and exit
                drain_state["error"] = e

        drainer = threading.Thread(
            target=_drain, name="runclass-drain", daemon=True)
        drainer.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _terminate_process_group(proc)
            drainer.join(timeout=5)
            raise JavaExecutionError(
                f"{class_name} timed out after {timeout}s"
                + (f"; full log: {log_path}" if log_path else "")
            ) from e
        finally:
            drainer.join(timeout=10)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass
            jvm_slot.release()

        if drain_state["error"] is not None:
            raise drain_state["error"]

        return proc.returncode, "".join(captured)

    # ---- Availability checks ----

    def check_java_available(self) -> bool:
        """Quick check: can javac run?"""
        try:
            self.find_java_executable("javac")
            return True
        except JavaNotFoundError:
            return False

    def check_comsol_available(self) -> bool:
        """Quick check: do COMSOL jars exist?"""
        return len(self.find_comsol_jars()) > 0


# ---- Module-level helpers ----

def _public_class_name(java_src: Path) -> str:
    """Best-effort public class name for a .java source.

    Java requires a public top-level class to share the file's basename,
    so the stem is the reliable fallback; we still scan for an explicit
    `public [final|abstract] class NAME` so an unconventionally-named
    file still runs. Topic-agnostic — pure source parsing.
    """
    try:
        text = java_src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return java_src.stem
    m = re.search(
        r"\bpublic\s+(?:final\s+|abstract\s+)?class\s+(\w+)", text)
    return m.group(1) if m else java_src.stem


def resolve_license_timeout(explicit: float | None) -> float | None:
    """Resolve the license-checkout timeout (seconds).

    Precedence: an explicit value wins; otherwise the COMSOL_LICENSE_TIMEOUT
    environment variable; otherwise None (disabled — wait indefinitely,
    preserving legacy behavior). A non-positive or unparseable value
    resolves to None. Topic-agnostic.
    """
    if explicit is not None:
        return explicit if explicit > 0 else None
    env = os.environ.get("COMSOL_LICENSE_TIMEOUT")
    if env:
        try:
            v = float(env)
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def _terminate_process_group(proc: subprocess.Popen, *, grace_s: int = 10) -> None:
    """Stop a subprocess and any children it spawned, cleanly.

    Sends SIGTERM to the whole process group first so a wrapped JVM gets
    a chance to close its sockets (critical for floating-license servers:
    a clean TCP close lets FlexNet reclaim the seat immediately, whereas a
    SIGKILL leaves a ghost checkout). Escalates to SIGKILL only after
    `grace_s`. Requires the process to have been launched with
    `start_new_session=True`; on non-POSIX platforms (or if the group
    signal fails) it falls back to terminate()/kill() on the process
    itself. Idempotent and never raises.
    """
    if proc.poll() is not None:
        return

    def _signal_group(sig: int) -> bool:
        if os.name != "posix":
            return False
        pid = getattr(proc, "pid", None)
        if pid is None:
            return False
        try:
            pgid = os.getpgid(pid)
            if pgid == os.getpgid(0):
                # The child shares our process group (caller forgot
                # start_new_session) — a group signal would hit us too.
                return False
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    if not _signal_group(signal.SIGTERM):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if not _signal_group(_SIGKILL):
        try:
            proc.kill()
        except Exception:
            pass


def describe_abnormal_exit(
    returncode: int | None,
    pid: int | None = None,
    search_dirs: list[Path] | None = None,
) -> str:
    """Human-readable forensics for an abnormal JVM exit.

    Decodes a negative subprocess returncode to its signal name (Popen
    reports signal death as -N: -6 = SIGABRT, -11 = SIGSEGV) and looks
    for a matching `hs_err_pid<pid>.log` JVM crash dump in the given
    directories (JVMs write it to their cwd). Both halves are
    best-effort; never raises. A large meshing campaign's SIGABRT was
    reported only as "exit=-6" with the dump — when one existed at all —
    left uncollected in whatever cwd the JVM had (G-SIGABRT-NO-HSERR:
    a native C++ abort may produce NO dump; say so rather than imply
    one should exist).
    """
    parts: list[str] = []
    try:
        if returncode is not None and returncode < 0:
            # Decode against the fixed POSIX numbering: a -N returncode
            # is produced by POSIX subprocess semantics, so the host's
            # signal table is the wrong reference (Windows lacks
            # SIGKILL and numbers SIGABRT as 22, yet must still decode
            # telemetry recorded on Linux).
            name = _POSIX_SIGNAMES.get(-returncode)
            if name is None:
                try:
                    name = signal.Signals(-returncode).name
                except ValueError:
                    name = f"signal {-returncode}"
            parts.append(
                f"process died on {name} (exit={returncode}) — a native "
                "crash or an external kill, not a Java exception"
            )
    except Exception:
        pass
    try:
        # Dump discovery only for abnormal exits (signal death, or an
        # unknown returncode): pids recycle, so matching a dump filename
        # against a pid that exited CLEANLY risks citing a stale dump
        # from an unrelated process.
        if pid is not None and (returncode is None or returncode < 0):
            dirs = list(search_dirs or [])
            if Path.cwd() not in dirs:
                dirs.append(Path.cwd())
            hits = [d / f"hs_err_pid{pid}.log" for d in dirs]
            found = [str(h) for h in hits if h.is_file()]
            if found:
                parts.append(f"JVM crash dump: {found[0]}")
            elif returncode is not None and returncode < 0:
                parts.append(
                    "no hs_err_pid dump found (a native abort can die "
                    "before the JVM crash handler runs — absence of a "
                    "dump is not absence of a native crash)"
                )
    except Exception:
        pass
    return "; ".join(parts)


def javac_diagnostics(stderr: str, limit: int = 20) -> str:
    """Strip javac's informational notes from compiler output.

    COMSOL's JARs carry annotation processors, so every javac run emits
    a six-line "Note: Annotation processing is enabled..." preamble.
    Reporting truncated raw stderr therefore shows only that note and
    hides the actual error — which is exactly how an install-blocking
    "cannot find symbol" was misread as a harmless warning.
    """
    keep: list[str] = []
    in_note = False
    for line in (stderr or "").splitlines():
        if line.startswith("Note:"):
            in_note = True
            continue
        # A note's continuation lines are indented; a real diagnostic
        # starts at column 0 (a path) or is javac's error summary.
        if in_note and (not line.strip() or line[:1].isspace()):
            continue
        in_note = False
        if line.strip():
            keep.append(line)
    return "\n".join(keep[:limit])


def find_comsol_launcher(comsol_path: str | Path) -> Path | None:
    """Locate the COMSOL launcher executable for this platform.

    Linux/macOS ship it at {root}/bin/comsol; Windows at
    {root}/bin/win64/comsol.exe. Returns None when absent (callers
    treat the launcher as optional — the Java API path via plugins/
    JARs does not need it).
    """
    root = Path(comsol_path)
    if _platform_subdir() == "win64":
        candidates = [
            root / "bin" / "win64" / "comsol.exe",
            root / "bin" / "comsol.exe",
        ]
    else:
        candidates = [root / "bin" / "comsol"]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _platform_subdir(comsol_path: str | Path | None = None) -> str:
    """Return the COMSOL platform subdirectory name.

    COMSOL uses: glnxa64 (Linux x86_64), maci64 (macOS Intel),
    macarm64 (macOS ARM — verified against a real 6.4 install, which
    ships bin/macarm64, lib/macarm64, java/macarm64), win64 (Windows).

    When ``comsol_path`` is given and more than one name is plausible,
    the install itself is probed (bin/, lib/, java/) so a rename in a
    future COMSOL release degrades gracefully instead of silently
    pointing at a nonexistent directory.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        candidates = ["glnxa64"]
    elif system == "darwin":
        if "arm" in machine or "aarch" in machine:
            candidates = ["macarm64", "maca64"]
        else:
            candidates = ["maci64"]
    elif system == "windows":
        candidates = ["win64"]
    else:
        candidates = ["glnxa64"]  # default fallback

    if comsol_path is not None and len(candidates) > 1:
        root = Path(comsol_path)
        for cand in candidates:
            for parent in ("bin", "lib", "java"):
                if (root / parent / cand).is_dir():
                    return cand
    return candidates[0]
