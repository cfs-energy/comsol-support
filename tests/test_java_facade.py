"""Tests for comsol_support.java_facade — Java compilation/execution wrapper.

All tests mock subprocess.run — no actual Java compilation.
"""

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comsol_support import COMSOL_PATH
from comsol_support.java_facade import (
    CompileResult,
    JavaFacade,
    JavaNotFoundError,
    FACADE_SOURCES,
    _platform_subdir,
    _public_class_name,
    _terminate_process_group,
)


# ---- Fixtures ----

@pytest.fixture
def facade(tmp_path):
    """JavaFacade with a temporary workspace and fake COMSOL path."""
    comsol_path = tmp_path / "comsol62"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return JavaFacade(
        comsol_path=str(comsol_path),
        workspace_dir=workspace,
    )


@pytest.fixture
def facade_with_jars(tmp_path):
    """JavaFacade with a fake COMSOL plugins directory containing jars."""
    comsol_path = tmp_path / "comsol62"
    plugins = comsol_path / "plugins"
    plugins.mkdir(parents=True)
    # Create fake COMSOL jars
    (plugins / "com.comsol.model_6.2.0.jar").touch()
    (plugins / "com.comsol.model.util_6.2.0.jar").touch()
    (plugins / "org.eclipse.osgi_3.18.jar").touch()  # non-COMSOL jar
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return JavaFacade(
        comsol_path=str(comsol_path),
        workspace_dir=workspace,
    )


# ---- TestFindJavaExecutable ----

class TestFindJavaExecutable:

    def test_finds_javac_on_path(self, facade):
        with patch("shutil.which", return_value="/usr/bin/javac"):
            result = facade.find_java_executable("javac")
        assert result == "/usr/bin/javac"

    def test_finds_javac_in_comsol(self, facade, tmp_path):
        comsol_jdk = (
            facade.comsol_path / "java" / _platform_subdir()
            / "jre" / "bin" / "javac"
        )
        comsol_jdk.parent.mkdir(parents=True)
        comsol_jdk.touch()
        with patch("shutil.which", return_value=None):
            result = facade.find_java_executable("javac")
        assert result == str(comsol_jdk)

    def test_raises_when_not_found(self, facade):
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(JavaNotFoundError, match="Cannot find"):
                    facade.find_java_executable("javac")

    def test_prefers_comsol_jdk_over_system(self, facade, tmp_path):
        comsol_jdk = (
            facade.comsol_path / "java" / _platform_subdir()
            / "jre" / "bin" / "javac"
        )
        comsol_jdk.parent.mkdir(parents=True)
        comsol_jdk.touch()
        with patch("shutil.which", return_value="/usr/bin/javac"):
            result = facade.find_java_executable("javac")
        # The bundled JDK is version-matched to the COMSOL jars; a
        # system javac of unknown vintage must not shadow it (macOS's
        # /usr/bin/javac shim was JDK 11 vs plugins built for 21).
        assert result == str(comsol_jdk)

    def test_finds_javac_in_macos_bundle_layout(self, facade):
        # macOS COMSOL ships the JDK as a bundle: jre/Contents/Home/bin
        comsol_jdk = (
            facade.comsol_path / "java" / _platform_subdir()
            / "jre" / "Contents" / "Home" / "bin" / "javac"
        )
        comsol_jdk.parent.mkdir(parents=True)
        comsol_jdk.touch()
        with patch("shutil.which", return_value=None):
            result = facade.find_java_executable("javac")
        assert result == str(comsol_jdk)


# ---- TestFindComsolJars ----

class TestFindComsolJars:

    def test_finds_all_jars_in_plugins(self, facade_with_jars):
        """All JAR files in plugins/ are returned (not just com.comsol.model*)."""
        jars = facade_with_jars.find_comsol_jars()
        assert len(jars) == 3  # model + model.util + eclipse.osgi
        names = [j.name for j in jars]
        assert "com.comsol.model_6.2.0.jar" in names
        assert "com.comsol.model.util_6.2.0.jar" in names
        assert "org.eclipse.osgi_3.18.jar" in names

    def test_returns_empty_when_no_plugins(self, facade):
        jars = facade.find_comsol_jars()
        assert jars == []

    def test_includes_api_jar(self, tmp_path):
        """Regression: com.comsol.api_1.0.0.jar must be included (classpath bug fix)."""
        comsol_path = tmp_path / "comsol64"
        plugins = comsol_path / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "com.comsol.api_1.0.0.jar").touch()
        (plugins / "com.comsol.model_1.0.0.jar").touch()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        facade = JavaFacade(str(comsol_path), workspace)
        jars = facade.find_comsol_jars()
        names = [j.name for j in jars]
        assert "com.comsol.api_1.0.0.jar" in names


# ---- TestClasspath ----

class TestClasspath:

    def test_facade_classpath_no_comsol(self, facade):
        cp = facade.get_facade_classpath()
        assert cp == str(facade.compiled_dir)
        assert "plugins" not in cp

    def test_full_classpath_includes_jars(self, facade_with_jars):
        cp = facade_with_jars.get_full_classpath()
        parts = cp.split(os.pathsep)
        assert parts[0] == str(facade_with_jars.compiled_dir)
        # Plugins are supplied as a single `plugins/*` classpath
        # wildcard (expanded natively by java/javac) rather than an
        # explicit JAR enumeration.
        assert parts[-1] == str(
            facade_with_jars.comsol_path / "plugins" / "*")
        assert len(parts) == 2

    def test_classpath_separator(self, facade_with_jars):
        cp = facade_with_jars.get_full_classpath()
        assert os.pathsep in cp


# ---- TestCompileFacade ----

class TestCompileFacade:

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_command_construction(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        facade.compile_facade()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/javac"
        assert "-d" in cmd
        assert str(facade.compiled_dir) in cmd
        # Check source files included
        for src in FACADE_SOURCES:
            assert any(src in arg for arg in cmd)

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_success_returns_true(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        result = facade.compile_facade()
        assert result.success is True
        assert isinstance(result, CompileResult)

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_failure_returns_false(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error: cannot find symbol"
        )
        result = facade.compile_facade()
        assert result.success is False
        assert "cannot find symbol" in result.stderr

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_compiles_to_correct_directory(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        facade.compile_facade()
        cmd = mock_run.call_args[0][0]
        d_idx = cmd.index("-d")
        assert cmd[d_idx + 1] == str(facade.compiled_dir)




# ---- TestCompileStageCode ----

class TestCompileStageCode:

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_includes_full_classpath(self, mock_which, mock_run,
                                     facade_with_jars):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        src = Path("/tmp/GeometryStage.java")
        facade_with_jars.compile_stage_code(src)
        cmd = mock_run.call_args[0][0]
        cp_idx = cmd.index("-cp")
        cp = cmd[cp_idx + 1]
        assert str(facade_with_jars.comsol_path / "plugins" / "*") in cp

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_source_path_passed(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        src = Path("/tmp/PhysicsStage.java")
        facade.compile_stage_code(src)
        cmd = mock_run.call_args[0][0]
        assert str(src) in cmd

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_returns_compile_result(self, mock_which, mock_run, facade):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        result = facade.compile_stage_code(Path("/tmp/Test.java"))
        assert isinstance(result, CompileResult)
        assert result.success is True
        assert result.stdout == "ok"




# ---- TestCheckAvailability ----

class TestCheckAvailability:

    def test_java_available_true(self, facade):
        with patch("shutil.which", return_value="/usr/bin/javac"):
            assert facade.check_java_available() is True

    def test_java_available_false(self, facade):
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {}, clear=True):
                assert facade.check_java_available() is False

    def test_comsol_available_true(self, facade_with_jars):
        assert facade_with_jars.check_comsol_available() is True

    def test_comsol_available_false(self, facade):
        assert facade.check_comsol_available() is False


# ---- TestJavaSourceFiles ----

class TestJavaSourceFiles:
    """Verify that the Java source artifacts exist and have expected content."""

    JAVA_DIR = Path(__file__).parent.parent / "comsol_support" / "java"

    def test_tag_registry_exists(self):
        assert (self.JAVA_DIR / "TagRegistry.java").is_file()

    def test_selection_algebra_exists(self):
        assert (self.JAVA_DIR / "SelectionAlgebra.java").is_file()


    def test_tag_registry_has_class(self):
        src = (self.JAVA_DIR / "TagRegistry.java").read_text()
        assert "public class TagRegistry" in src

    def test_selection_algebra_has_class(self):
        src = (self.JAVA_DIR / "SelectionAlgebra.java").read_text()
        assert "public class SelectionAlgebra" in src


    def test_tag_registry_no_comsol_import(self):
        src = (self.JAVA_DIR / "TagRegistry.java").read_text()
        assert "import com.comsol" not in src

    def test_selection_algebra_no_comsol_import(self):
        src = (self.JAVA_DIR / "SelectionAlgebra.java").read_text()
        assert "import com.comsol" not in src

    def test_tag_registry_has_serialize(self):
        src = (self.JAVA_DIR / "TagRegistry.java").read_text()
        assert "serialize()" in src
        assert "deserialize(" in src

    def test_selection_algebra_has_boolean_ops(self):
        src = (self.JAVA_DIR / "SelectionAlgebra.java").read_text()
        for method in ["createUnion", "createIntersection",
                       "createComplement", "createDifference",
                       "createAdjacent"]:
            assert method in src, f"Missing method: {method}"


# ---- TestPlatformSubdir ----

class TestPlatformSubdir:

    @patch("platform.system", return_value="Linux")
    @patch("platform.machine", return_value="x86_64")
    def test_linux(self, mock_machine, mock_system):
        assert _platform_subdir() == "glnxa64"

    @patch("platform.system", return_value="Darwin")
    @patch("platform.machine", return_value="arm64")
    def test_macos_arm(self, mock_machine, mock_system):
        # Real 6.4 Apple Silicon installs ship bin/lib/java under
        # macarm64 (not the maca64 the port originally guessed).
        assert _platform_subdir() == "macarm64"

    @patch("platform.system", return_value="Darwin")
    @patch("platform.machine", return_value="arm64")
    def test_macos_arm_probes_install(self, mock_machine, mock_system,
                                      tmp_path):
        # With a comsol_path given, the install layout wins over the
        # static default — a hypothetical maca64 install still resolves.
        (tmp_path / "lib" / "maca64").mkdir(parents=True)
        assert _platform_subdir(tmp_path) == "maca64"
        # And a macarm64 install resolves to macarm64.
        (tmp_path / "bin" / "macarm64").mkdir(parents=True)
        assert _platform_subdir(tmp_path) == "macarm64"

    @patch("platform.system", return_value="Darwin")
    @patch("platform.machine", return_value="x86_64")
    def test_macos_intel(self, mock_machine, mock_system):
        assert _platform_subdir() == "maci64"

    @patch("platform.system", return_value="Windows")
    @patch("platform.machine", return_value="AMD64")
    def test_windows(self, mock_machine, mock_system):
        assert _platform_subdir() == "win64"


# ---- TestPublicClassName ----

class TestPublicClassName:
    def test_matches_filename_stem_by_convention(self, tmp_path):
        src = tmp_path / "DiagProbe.java"
        src.write_text("public class DiagProbe { public static void main(String[] a){} }")
        assert _public_class_name(src) == "DiagProbe"

    def test_parses_explicit_name_when_unconventional(self, tmp_path):
        # Non-public helper above the public class must not win.
        src = tmp_path / "weird_name.java"
        src.write_text("class Helper {}\npublic final class RealClass {}\n")
        assert _public_class_name(src) == "RealClass"

    def test_falls_back_to_stem_when_unparseable(self, tmp_path):
        src = tmp_path / "Mystery.java"
        src.write_text("// no class decl here\n")
        assert _public_class_name(src) == "Mystery"

    def test_missing_file_falls_back_to_stem(self, tmp_path):
        assert _public_class_name(tmp_path / "Gone.java") == "Gone"


# ---- TestTerminateProcessGroup ----

class TestTerminateProcessGroup:
    def test_noop_when_already_exited(self):
        proc = MagicMock()
        proc.poll.return_value = 0  # already dead
        # Should return without attempting any signal / wait.
        _terminate_process_group(proc)
        proc.wait.assert_not_called()

    def test_escalates_to_group_kill_after_grace(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4321
        # First wait (after SIGTERM) times out -> escalate to SIGKILL.
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        # The child leads its own group (4321); our group (getpgid(0))
        # differs — group signalling is therefore safe and expected.
        # create=True: getpgid/killpg don't exist in the Windows os
        # module, and the code path is forced to posix via os.name.
        with patch("os.getpgid", create=True,
                   side_effect=lambda p: 4321 if p == 4321 else 1), \
             patch("os.killpg", create=True) as killpg, \
             patch("os.name", "posix"):
            _terminate_process_group(proc, grace_s=0)
        sigs = [call.args[1] for call in killpg.call_args_list]
        # SIGKILL is absent from the Windows signal module; 9 is its
        # universal number (matches java_facade._SIGKILL).
        assert signal.SIGTERM in sigs
        assert getattr(signal, "SIGKILL", 9) in sigs

    def test_refuses_group_kill_when_sharing_callers_group(self):
        """A child that shares our process group (caller forgot
        start_new_session) must never be group-signalled — that would
        SIGTERM the caller too. Falls back to per-process kill."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4321
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        with patch("os.getpgid", create=True, return_value=7777), \
             patch("os.killpg", create=True) as killpg, \
             patch("os.name", "posix"):
            _terminate_process_group(proc, grace_s=0)
        killpg.assert_not_called()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ---- TestRunClass ----

class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.pid = 999

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class TestRunClass:
    @patch("comsol_support.java_facade.subprocess.Popen")
    def test_bare_class_runs_without_compile_and_tees_log(
        self, mock_popen, facade, tmp_path
    ):
        mock_popen.return_value = _FakeProc(
            ["solver line 1\n", "solver line 2\n"], returncode=0)
        log = tmp_path / "out.log"
        with patch.object(facade, "find_java_executable", return_value="java"), \
             patch.object(facade, "get_full_classpath", return_value="CP"), \
             patch.object(facade, "get_comsol_env", return_value={}):
            rc, out = facade.run_class(
                "SomeClass", ["a", "b"], log_path=log)
        assert rc == 0
        assert out == "solver line 1\nsolver line 2\n"
        # Full unfiltered output is teed to the log.
        assert log.read_text() == "solver line 1\nsolver line 2\n"
        # Command construction: java <jvm_args> -cp CP SomeClass a b
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "java"
        assert cmd[-3:] == ["SomeClass", "a", "b"]
        assert "-cp" in cmd and "CP" in cmd
        # Launched in its own session so the group can be killed.
        assert mock_popen.call_args[1]["start_new_session"] == (os.name == "posix")

    @patch("comsol_support.java_facade.subprocess.Popen")
    def test_console_filter_limits_mirror_not_log(
        self, mock_popen, facade, tmp_path
    ):
        mock_popen.return_value = _FakeProc(
            ["KEEP me\n", "drop me\n", "KEEP this\n"], returncode=0)
        log = tmp_path / "out.log"

        class _Sink:
            def __init__(self): self.buf = ""
            def write(self, s): self.buf += s
            def flush(self): pass

        sink = _Sink()
        with patch.object(facade, "find_java_executable", return_value="java"), \
             patch.object(facade, "get_full_classpath", return_value="CP"), \
             patch.object(facade, "get_comsol_env", return_value={}):
            rc, out = facade.run_class(
                "C", log_path=log, mirror=sink,
                console_filter=lambda l: l.startswith("KEEP"))
        # Log keeps everything; console mirror only KEEP lines.
        assert log.read_text() == "KEEP me\ndrop me\nKEEP this\n"
        assert sink.buf == "KEEP me\nKEEP this\n"


# ---- TestCompileComsolSources ----

class TestCompileComsolSources:
    """Compiling the package's own COMSOL-dependent Java.

    Regression: the installers compiled these one at a
    time in alphabetical order. ModelChecker references SlotHarvester
    and ModelExporter references SolverTelemetry — both sort later —
    and javac defaults its source path to the *class* path, where the
    .java files are not. Every first install therefore died with
    "cannot find symbol" on a clean workspace.
    """

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_single_class_compile_passes_sourcepath(
        self, mock_which, mock_run, facade_with_jars,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        facade_with_jars.compile_comsol_class("ModelChecker.java")
        cmd = mock_run.call_args[0][0]
        assert "-sourcepath" in cmd
        assert cmd[cmd.index("-sourcepath") + 1] == str(
            facade_with_jars.java_source_dir)

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_compiles_every_source_in_one_invocation(
        self, mock_which, mock_run, facade_with_jars, tmp_path,
    ):
        src = tmp_path / "java_src"
        src.mkdir()
        for name in ("Alpha.java", "Zulu.java"):
            (src / name).write_text("class X {}")
        facade_with_jars.java_source_dir = src
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = facade_with_jars.compile_comsol_sources()

        assert result.success
        assert mock_run.call_count == 1, "one javac call, not one per file"
        cmd = mock_run.call_args[0][0]
        assert str(src / "Alpha.java") in cmd
        assert str(src / "Zulu.java") in cmd

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/javac")
    def test_no_sources_is_success_without_running_javac(
        self, mock_which, mock_run, facade_with_jars, tmp_path,
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        facade_with_jars.java_source_dir = empty
        assert facade_with_jars.compile_comsol_sources().success
        mock_run.assert_not_called()


class TestJavacDiagnostics:
    """COMSOL's JARs carry annotation processors, so every javac run
    emits a multi-line informational note. Truncated raw stderr showed
    only that note and hid the real error underneath it."""

    def test_strips_annotation_processing_note(self):
        from comsol_support.java_facade import javac_diagnostics

        stderr = (
            "Note: Annotation processing is enabled because one or more\n"
            "  processors were found on the class path.\n"
            "  Use -proc:none to disable annotation processing.\n"
            "/src/ModelChecker.java:42: error: cannot find symbol\n"
            "  symbol:   variable SlotHarvester\n"
            "2 errors\n"
        )
        out = javac_diagnostics(stderr)
        assert "Annotation processing" not in out
        assert "cannot find symbol" in out
        assert "2 errors" in out

    def test_empty_and_note_only_inputs(self):
        from comsol_support.java_facade import javac_diagnostics

        assert javac_diagnostics("") == ""
        assert javac_diagnostics("Note: something\n  indented\n") == ""


@pytest.mark.real_comsol
@pytest.mark.skipif(not Path(COMSOL_PATH).is_dir(),
                    reason=f"COMSOL not installed at {COMSOL_PATH}")
def test_dependent_source_compiles_alone_into_a_clean_workspace(tmp_path):
    """The exact first-install failure, against the real classpath.

    ModelChecker alone, nothing pre-compiled — this failed before
    -sourcepath was added.
    """
    from comsol_support.java_facade import javac_diagnostics

    facade = JavaFacade(comsol_path=COMSOL_PATH, workspace_dir=tmp_path)
    result = facade.compile_comsol_class("ModelChecker.java")
    assert result.success, javac_diagnostics(result.stderr)
