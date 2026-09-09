"""Tests for comsol_support.edit_mph — unit tests for contract detection,
classname extraction, CLI wiring, and error paths. Real-COMSOL E2E is
left to the gated test in test_real_comsol_integration.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comsol_support.cli import build_parser
from comsol_support.edit_mph import (
    EditMphResult,
    edit_mph,
    has_mutator_contract,
    has_query_contract,
)
from comsol_support.mphgen import (
    ContractError,
    MphgenError,
    MphValidationError,
    extract_public_class_name,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mphgen"
IDENTITY_MUTATOR = FIXTURE_DIR / "IdentityMutator.java"
BAD_MUTATOR = FIXTURE_DIR / "BadMutator.java"


# ---- Contract detection ----------------------------------------------------


def test_mutator_contract_detection_positive(tmp_path):
    src = tmp_path / "Good.java"
    src.write_text(
        "public class Good {\n"
        "    public static Model mutate(Model m, Map<String,String> args) {\n"
        "        return m;\n"
        "    }\n"
        "}\n"
    )
    assert has_mutator_contract(src)


def test_mutator_contract_detection_negative(tmp_path):
    src = tmp_path / "Bad.java"
    src.write_text(
        "public class Bad {\n"
        "    public static Model build(Map<String,String> args) {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    assert not has_mutator_contract(src)


def test_mutator_contract_on_real_fixture():
    assert has_mutator_contract(IDENTITY_MUTATOR)
    assert not has_mutator_contract(BAD_MUTATOR)


def test_query_contract_detection_positive(tmp_path):
    src = tmp_path / "Probe.java"
    src.write_text(
        "public class Probe {\n"
        "    public static Map<String,Object> query("
        "Model m, Map<String,String> args) {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    assert has_query_contract(src)
    # A mutator is not a query.
    assert not has_query_contract(IDENTITY_MUTATOR)


def test_query_contract_detection_negative(tmp_path):
    src = tmp_path / "NotQuery.java"
    src.write_text(
        "public class NotQuery {\n"
        "    public static Model mutate(Model m, Map<String,String> a){"
        "return m;}\n"
        "}\n"
    )
    assert not has_query_contract(src)


def test_mutator_class_name_extracted_via_shared_helper():
    """edit_mph reuses mphgen.extract_public_class_name — pin the integration."""
    assert extract_public_class_name(IDENTITY_MUTATOR) == "IdentityMutator"
    assert extract_public_class_name(BAD_MUTATOR) == "BadMutator"


# ---- Validation guards (pre-JVM) ------------------------------------------


def test_edit_mph_rejects_missing_input(tmp_path):
    out = tmp_path / "out.mph"
    with pytest.raises(MphgenError, match="not found"):
        edit_mph(input_mph=tmp_path / "nonexistent.mph", output_mph=out)


def test_edit_mph_rejects_non_mph_input(tmp_path):
    bogus = tmp_path / "bogus.mph"
    bogus.write_bytes(b"not a zip file")
    out = tmp_path / "out.mph"
    with pytest.raises(MphValidationError):
        edit_mph(input_mph=bogus, output_mph=out)


def test_edit_mph_rejects_existing_output_without_force(tmp_path):
    # Make input look superficially valid enough to pass the file check;
    # we'll be stopped by validate_mph but the OUTPUT-exists check fires
    # first if we set things up correctly.
    inp = tmp_path / "in.mph"
    inp.write_bytes(b"")  # validate_mph will reject this — but the output
    out = tmp_path / "out.mph"
    out.write_bytes(b"existing")
    # Output-exists fires BEFORE validate_mph in our flow:
    with pytest.raises(MphgenError, match="already exists"):
        edit_mph(input_mph=inp, output_mph=out)


def test_edit_mph_rejects_bad_mutator_contract(tmp_path):
    """Mutator without `static Model mutate(Model, Map<String,String>)` is
    rejected before any JVM launch."""
    # Build a dummy input that will pass file-existence but fail validate_mph;
    # we test that ContractError fires by passing a valid-zip input.
    import zipfile
    inp = tmp_path / "in.mph"
    # Pad past validate_mph's 1024-byte size floor so we reach the
    # ContractError path instead of bouncing on size.
    bulk = "x" * 2048
    with zipfile.ZipFile(inp, "w") as zf:
        zf.writestr("dmodel.xml", "<dummy/>" + bulk)
        zf.writestr("model.xml", "<dummy/>" + bulk)
        zf.writestr("fileversion", "1")
    out = tmp_path / "out.mph"
    with pytest.raises(ContractError, match="mutate"):
        edit_mph(
            input_mph=inp, output_mph=out,
            mutator_java=BAD_MUTATOR,
        )


# ---- CLI wiring -----------------------------------------------------------


def test_editmph_subparser_registered_in_main_cli():
    parser = build_parser()
    # Argparse: ensure `edit-mph` is reachable.
    args = parser.parse_args(["edit-mph", "--input", "in.mph",
                              "--output", "out.mph"])
    assert args.command == "edit-mph"
    assert args.input == "in.mph"
    assert args.output == "out.mph"
    assert args.mutator is None
    assert args.solve is None


def test_editmph_subparser_accepts_full_arg_set():
    parser = build_parser()
    args = parser.parse_args([
        "edit-mph",
        "--input", "in.mph",
        "--mutator", "Foo.java",
        "--output", "out.mph",
        "--arg", "k1=v1",
        "--arg", "k2=v2",
        "--solve", "std1",
        "--timeout", "300",
        "--force",
        "--no-sidecar",
        "--no-lint",
        "--skip-layer-b",
    ])
    assert args.mutator == "Foo.java"
    assert args.arg == ["k1=v1", "k2=v2"]
    assert args.solve == "std1"
    assert args.timeout == 300
    assert args.force is True
    assert args.no_sidecar is True
    assert args.no_lint is True
    assert args.skip_layer_b is True


def test_editmph_subparser_requires_input_and_output():
    """Missing --input or --output must fail at argparse time."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["edit-mph", "--output", "x.mph"])
    with pytest.raises(SystemExit):
        parser.parse_args(["edit-mph", "--input", "x.mph"])


def test_editmph_subparser_accepts_no_save():
    parser = build_parser()
    args = parser.parse_args([
        "edit-mph", "--input", "in.mph", "--output", "out.mph", "--no-save",
    ])
    assert args.no_save is True


def test_query_mph_subparser_registered_in_main_cli():
    parser = build_parser()
    args = parser.parse_args([
        "query-mph", "--input", "in.mph", "--query", "Probe.java",
        "--arg", "k=v", "--solve", "std1",
    ])
    assert args.command == "query-mph"
    assert args.input == "in.mph"
    assert args.query == "Probe.java"
    assert args.arg == ["k=v"]
    assert args.solve == "std1"
    assert args.output is None  # optional in no-save mode


def test_query_mph_subparser_requires_input_and_query():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["query-mph", "--input", "in.mph"])
    with pytest.raises(SystemExit):
        parser.parse_args(["query-mph", "--query", "Q.java"])


# ---- Result dataclass -----------------------------------------------------


def test_edit_mph_result_dataclass_shape():
    """Smoke: EditMphResult has the expected field set so callers can rely
    on the shape without instantiating a real run."""
    r = EditMphResult(
        success=True, input_mph="a", mutator_java=None,
        mutator_class=None, output_mph="b",
    )
    assert r.success is True
    assert r.mutator_args == {}
    assert r.telemetry_digest == {}
    assert r.solved is False


# ---- In-place edit atomicity (mocked JVM) -----------------------------------


def _patched_facade():
    """Context-manager stack mirroring test_mphgen's happy-path patching."""
    import os
    from unittest.mock import MagicMock, patch
    jf = __import__("comsol_support.java_facade", fromlist=["JavaFacade"]).JavaFacade
    return [
        patch.object(jf, "find_java_executable", return_value="/fake/java"),
        patch.object(jf, "get_full_classpath", return_value="/fake/cp"),
        patch.object(jf, "get_comsol_env", return_value=os.environ.copy()),
        patch.object(jf, "compile_stage_code",
                     return_value=MagicMock(success=True, stderr="")),
    ]


def _fake_edit_run(success: bool = True, write_output: bool = True):
    """subprocess.Popen side_effect: write a fake .mph at the argv
    --output path and emit a JSON envelope."""
    import json as _json
    from tests.conftest import FakePopen
    from tests.test_mphgen import _make_fake_mph

    def side_effect(argv, **kwargs):
        if "-Djava.awt.headless=true" not in argv:
            return FakePopen(stdout_text="", returncode=0)
        out = Path(argv[argv.index("--output") + 1])
        if write_output:
            _make_fake_mph(out)
        if success:
            env = {"success": True, "output": str(out), "solved": False}
        else:
            env = {"success": False, "error": "synthetic failure",
                   "stack_trace": ""}
        return FakePopen(stdout_text=_json.dumps(env) + "\n",
                         returncode=0 if success else 1)
    return side_effect


def _enter_all(stack, patches):
    for p in patches:
        stack.enter_context(p)


def test_edit_mesh_and_jvm_args_reach_the_jvm_argv(tmp_path):
    """--mesh forwards as ModelExporter --mesh <tag>; user jvm_args land
    after the headless flag."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from tests.test_mphgen import _make_fake_mph
    import json as _json
    from tests.conftest import FakePopen

    mph = tmp_path / "model.mph"
    _make_fake_mph(mph)
    out = tmp_path / "out.mph"

    compiled_dir = tmp_path / "java_compiled"
    compiled_dir.mkdir()
    (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
    (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")

    seen_argv: list = []

    def spy(argv, **kwargs):
        if "-Djava.awt.headless=true" not in argv:
            return FakePopen(stdout_text="", returncode=0)
        seen_argv.extend(argv)
        _make_fake_mph(Path(argv[argv.index("--output") + 1]))
        env = {"success": True, "output": argv[argv.index("--output") + 1],
               "solved": False, "mesh_tag": "mesh1", "mesh_success": True}
        return FakePopen(stdout_text=_json.dumps(env) + "\n", returncode=0)

    with ExitStack() as stack:
        _enter_all(stack, _patched_facade())
        stack.enter_context(patch(
            "comsol_support.edit_mph.subprocess.Popen", side_effect=spy))
        result = edit_mph(
            input_mph=mph, output_mph=out,
            mesh_tag="mesh1", jvm_args=["-Xmx96g"],
            workspace_dir=tmp_path, run_linting=False,
        )

    assert result.success is True
    # --mesh forwarded with its tag
    assert "--mesh" in seen_argv
    assert seen_argv[seen_argv.index("--mesh") + 1] == "mesh1"
    # jvm arg placed after the headless flag, before -cp
    assert seen_argv.index("-Xmx96g") > seen_argv.index(
        "-Djava.awt.headless=true")
    assert seen_argv.index("-Xmx96g") < seen_argv.index("-cp")
    # envelope mesh fields surface on the result
    assert result.mesh_tag == "mesh1"
    assert result.mesh_success is True


def test_edit_writes_crash_safe_jvm_log(tmp_path):
    """Every edit run tees the full raw JVM stream to
    <output>.mph.jvm.log; the result records the path."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from tests.test_mphgen import _make_fake_mph

    mph = tmp_path / "model.mph"
    _make_fake_mph(mph)
    out = tmp_path / "out.mph"

    compiled_dir = tmp_path / "java_compiled"
    compiled_dir.mkdir()
    (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
    (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")

    with ExitStack() as stack:
        _enter_all(stack, _patched_facade())
        stack.enter_context(patch(
            "comsol_support.edit_mph.subprocess.Popen",
            side_effect=_fake_edit_run(success=True)))
        result = edit_mph(
            input_mph=mph, output_mph=out,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert result.success is True
    assert result.jvm_log_path.endswith(".mph.jvm.log")
    log = Path(result.jvm_log_path)
    assert log.exists()
    # The tee carries the full raw stream — here the fake JVM's envelope.
    assert '"success"' in log.read_text()
    # And it lands in the provenance sidecar via asdict().
    import json as _json
    sidecar = _json.loads(Path(result.sidecar_path).read_text())
    assert sidecar["jvm_log_path"] == result.jvm_log_path


def test_inplace_edit_replaces_input_atomically(tmp_path):
    """output == input routes the JVM write to a temp sibling which is
    validated then renamed over the input; no temp file survives."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from tests.test_mphgen import _make_fake_mph

    mph = tmp_path / "model.mph"
    _make_fake_mph(mph)
    before = mph.read_bytes()

    compiled_dir = tmp_path / "java_compiled"
    compiled_dir.mkdir()
    (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
    (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")

    with ExitStack() as stack:
        _enter_all(stack, _patched_facade())
        stack.enter_context(patch(
            "comsol_support.edit_mph.subprocess.Popen",
            side_effect=_fake_edit_run(success=True)))
        result = edit_mph(
            input_mph=mph, output_mph=mph,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert result.success is True
    assert mph.exists()
    assert mph.read_bytes() != before  # content was replaced
    leftovers = list(tmp_path.glob("*.inplace-tmp.mph"))
    assert leftovers == [], f"temp output not cleaned up: {leftovers}"
    # Result provenance reflects the final (renamed) file.
    assert result.output_bytes == mph.stat().st_size


def test_inplace_edit_failure_preserves_input(tmp_path):
    """A failing JVM run must leave the input byte-identical and must
    not leave the temp output behind."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from comsol_support.mphgen import BuildFailure
    from tests.test_mphgen import _make_fake_mph

    mph = tmp_path / "model.mph"
    _make_fake_mph(mph)
    before = mph.read_bytes()

    compiled_dir = tmp_path / "java_compiled"
    compiled_dir.mkdir()
    (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
    (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")

    with ExitStack() as stack:
        _enter_all(stack, _patched_facade())
        stack.enter_context(patch(
            "comsol_support.edit_mph.subprocess.Popen",
            side_effect=_fake_edit_run(success=False, write_output=True)))
        with pytest.raises(BuildFailure, match="synthetic failure"):
            edit_mph(
                input_mph=mph, output_mph=mph,
                workspace_dir=tmp_path, run_linting=False,
            )

    assert mph.read_bytes() == before  # input untouched
    assert list(tmp_path.glob("*.inplace-tmp.mph")) == []


def test_non_inplace_edit_writes_output_directly(tmp_path):
    """output != input keeps the existing direct-write behavior (no
    temp redirection)."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from tests.test_mphgen import _make_fake_mph

    src = tmp_path / "in.mph"
    dst = tmp_path / "out.mph"
    _make_fake_mph(src)

    compiled_dir = tmp_path / "java_compiled"
    compiled_dir.mkdir()
    (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
    (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")

    seen_outputs = []

    def spy_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            seen_outputs.append(argv[argv.index("--output") + 1])
        return _fake_edit_run(success=True)(argv, **kwargs)

    with ExitStack() as stack:
        _enter_all(stack, _patched_facade())
        stack.enter_context(patch(
            "comsol_support.edit_mph.subprocess.Popen", side_effect=spy_run))
        result = edit_mph(
            input_mph=src, output_mph=dst,
            workspace_dir=tmp_path, run_linting=False,
        )

    assert result.success is True
    assert seen_outputs == [str(dst)]
    assert dst.exists()
