"""Tests for comsol_support.mphgen — unit tests + one real-COMSOL E2E."""

import json
import os
import shutil
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comsol_support import COMSOL_PATH
from comsol_support.cli import build_parser
from comsol_support.mphgen import (
    ContractError,
    MphValidationError,
    MphgenError,
    REQUIRED_MPH_MEMBERS,
    extract_public_class_name,
    generate_mph,
    has_build_model_contract,
    validate_mph,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mphgen"
SIMPLE_BOX = FIXTURE_DIR / "SimpleBoxBuilder.java"


# ---- Contract detection ------------------------------------------------

def test_contract_detection_positive(tmp_path):
    src = tmp_path / "Good.java"
    src.write_text(
        "public class Good {\n"
        "    public static Model buildModel(Map<String,String> args) {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    assert has_build_model_contract(src)


def test_contract_detection_negative(tmp_path):
    src = tmp_path / "Legacy.java"
    src.write_text(
        "public class Legacy {\n"
        "    public static void main(String[] args) { }\n"
        "    static double runCase(double i) { return 0; }\n"
        "}\n"
    )
    assert not has_build_model_contract(src)


def test_contract_detection_with_generics_variants(tmp_path):
    # Slight whitespace / formatting variants should still match.
    src = tmp_path / "Variant.java"
    src.write_text(
        "public static  Model  buildModel( Map<String, String>  args )  {\n"
    )
    assert has_build_model_contract(src)


def test_contract_detection_on_real_fixture():
    assert has_build_model_contract(SIMPLE_BOX)


# ---- Classname extraction ----------------------------------------------

def test_extract_class_name_standard(tmp_path):
    src = tmp_path / "Foo.java"
    src.write_text("public class Foo { }\n")
    assert extract_public_class_name(src) == "Foo"


def test_extract_class_name_with_generics(tmp_path):
    src = tmp_path / "Bar.java"
    src.write_text("public class Bar<T> extends Baz { }\n")
    assert extract_public_class_name(src) == "Bar"


def test_extract_class_name_falls_back_to_stem(tmp_path):
    src = tmp_path / "NoDecl.java"
    src.write_text("// no public class declaration\n")
    assert extract_public_class_name(src) == "NoDecl"


def test_extract_class_name_on_real_fixture():
    assert extract_public_class_name(SIMPLE_BOX) == "SimpleBoxBuilder"


# ---- .mph validation ---------------------------------------------------

def _make_fake_mph(path: Path, members: set[str] | None = None,
                   min_bytes: int = 2048) -> None:
    """Write a zip file at `path` containing `members`.

    Uses ZIP_STORED (no compression) and incompressible random bytes so
    the on-disk file exceeds min_bytes regardless of compression.
    """
    members = REQUIRED_MPH_MEMBERS if members is None else members
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for i, name in enumerate(sorted(members)):
            size = min_bytes if i == 0 else 16
            zf.writestr(name, os.urandom(size))


def test_validate_mph_accepts_required_members(tmp_path):
    mph = tmp_path / "ok.mph"
    _make_fake_mph(mph)
    validate_mph(mph)  # no raise


def test_validate_mph_rejects_missing_member(tmp_path):
    mph = tmp_path / "bad.mph"
    _make_fake_mph(mph, members={"dmodel.xml", "model.xml"})  # missing fileversion
    with pytest.raises(MphValidationError, match="missing required"):
        validate_mph(mph)


def test_validate_mph_rejects_small_file(tmp_path):
    mph = tmp_path / "tiny.mph"
    mph.write_bytes(b"not a mph")
    with pytest.raises(MphValidationError, match="suspiciously small"):
        validate_mph(mph)


def test_validate_mph_rejects_nonzip(tmp_path):
    mph = tmp_path / "bogus.mph"
    mph.write_bytes(b"x" * 4096)  # large enough to pass size gate, not a zip
    with pytest.raises(MphValidationError, match="valid zip"):
        validate_mph(mph)


def test_validate_mph_rejects_missing(tmp_path):
    mph = tmp_path / "absent.mph"
    with pytest.raises(MphValidationError, match="not written"):
        validate_mph(mph)


# ---- generate_mph (unit, mocked JavaFacade) ----------------------------

def _simulate_java_run(output_mph: Path):
    """Factory: a subprocess.Popen side_effect that writes a fake .mph
    and returns a FakePopen with a success JSON envelope on stdout."""
    from tests.conftest import FakePopen

    def side_effect(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            _make_fake_mph(output_mph)
            stdout = json.dumps({
                "success": True,
                "output": str(output_mph),
                "builder": "SimpleBoxBuilder",
                "elapsed_ms": 42,
            }) + "\n"
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)
    return side_effect


def test_generate_mph_happy_path(tmp_path):
    """Fully mocked happy path — no COMSOL involved."""
    builder = tmp_path / "SimpleBoxBuilder.java"
    builder.write_text(SIMPLE_BOX.read_text())
    output = tmp_path / "out.mph"

    fake_run = _simulate_java_run(output)

    with patch("comsol_support.mphgen.subprocess.Popen", side_effect=fake_run), \
         patch.object(
             __import__("comsol_support.java_facade", fromlist=["JavaFacade"])
             .JavaFacade,
             "find_java_executable",
             return_value="/fake/java",
         ), \
         patch.object(
             __import__("comsol_support.java_facade", fromlist=["JavaFacade"])
             .JavaFacade,
             "get_full_classpath",
             return_value="/fake/cp",
         ), \
         patch.object(
             __import__("comsol_support.java_facade", fromlist=["JavaFacade"])
             .JavaFacade,
             "get_comsol_env",
             return_value=os.environ.copy(),
         ), \
         patch.object(
             __import__("comsol_support.java_facade", fromlist=["JavaFacade"])
             .JavaFacade,
             "compile_stage_code",
             return_value=MagicMock(success=True, stderr=""),
         ):
        # Pre-create exporter + telemetry .class so the recompile-check short-circuits.
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        result = generate_mph(
            builder_java=builder,
            output_mph=output,
            builder_args={"size_m": "0.02"},
            workspace_dir=tmp_path,
        )

    assert result.success is True
    assert result.builder_class == "SimpleBoxBuilder"
    assert result.output_bytes > 0
    assert len(result.output_sha256) == 64
    assert result.builder_args == {"size_m": "0.02"}
    sidecar = output.with_suffix(".mph.mphgen.json")
    assert sidecar.exists()
    sidecar_data = json.loads(sidecar.read_text())
    assert sidecar_data["success"] is True
    assert sidecar_data["builder_class"] == "SimpleBoxBuilder"


def test_generate_mph_rejects_missing_builder(tmp_path):
    with pytest.raises(MphgenError, match="not found"):
        generate_mph(
            builder_java=tmp_path / "NotThere.java",
            output_mph=tmp_path / "out.mph",
            workspace_dir=tmp_path,
        )


def test_generate_mph_rejects_non_java(tmp_path):
    notjava = tmp_path / "foo.txt"
    notjava.write_text("not java")
    with pytest.raises(MphgenError, match="must be a .java"):
        generate_mph(
            builder_java=notjava,
            output_mph=tmp_path / "out.mph",
            workspace_dir=tmp_path,
        )


def test_generate_mph_rejects_existing_output_without_force(tmp_path):
    builder = tmp_path / "SimpleBoxBuilder.java"
    builder.write_text(SIMPLE_BOX.read_text())
    output = tmp_path / "existing.mph"
    output.write_bytes(b"preexisting")
    with pytest.raises(MphgenError, match="already exists"):
        generate_mph(
            builder_java=builder, output_mph=output,
            workspace_dir=tmp_path,
        )


def test_generate_mph_rejects_legacy_contract(tmp_path):
    legacy = tmp_path / "Legacy.java"
    legacy.write_text(
        "public class Legacy {\n"
        "    public static void main(String[] args) { }\n"
        "    static double runCase(double i) { return 0; }\n"
        "}\n"
    )
    with pytest.raises(ContractError, match="buildModel"):
        generate_mph(
            builder_java=legacy,
            output_mph=tmp_path / "out.mph",
            workspace_dir=tmp_path,
        )


# ---- CLI wiring --------------------------------------------------------

def test_cli_mphgen_subparser_present():
    parser = build_parser()
    ns = parser.parse_args(["mphgen", "--builder", "foo.java"])
    assert ns.command == "mphgen"
    assert ns.builder == "foo.java"
    assert ns.arg == []
    assert ns.force is False


def test_cli_mphgen_accepts_repeated_args():
    parser = build_parser()
    ns = parser.parse_args([
        "mphgen", "--builder", "foo.java",
        "--arg", "a=1", "--arg", "b=2",
    ])
    assert ns.arg == ["a=1", "b=2"]


def test_cli_mphgen_solve_flag_defaults_none():
    parser = build_parser()
    ns = parser.parse_args(["mphgen", "--builder", "foo.java"])
    assert ns.solve is None


def test_cli_mphgen_solve_flag_takes_study_tag():
    parser = build_parser()
    ns = parser.parse_args([
        "mphgen", "--builder", "foo.java", "--solve", "std1",
    ])
    assert ns.solve == "std1"


def test_generate_mph_forwards_solve_flag(tmp_path):
    """--solve propagates to the ModelExporter argv."""
    builder = tmp_path / "SimpleBoxBuilder.java"
    builder.write_text(SIMPLE_BOX.read_text())
    output = tmp_path / "out.mph"
    captured = {}

    from tests.conftest import FakePopen

    def fake_run(argv, **kwargs):
        if "-Djava.awt.headless=true" in argv:
            captured["argv"] = list(argv)
            _make_fake_mph(output)
            stdout = json.dumps({
                "success": True, "output": str(output),
                "builder": "SimpleBoxBuilder",
                "solved": True, "study": "std1", "elapsed_ms": 1,
            }) + "\n"
            return FakePopen(stdout_text=stdout, returncode=0)
        return FakePopen(stdout_text="", returncode=0)

    jf = __import__("comsol_support.java_facade",
                    fromlist=["JavaFacade"]).JavaFacade
    with patch("comsol_support.mphgen.subprocess.Popen", side_effect=fake_run), \
         patch.object(jf, "find_java_executable", return_value="/fake/java"), \
         patch.object(jf, "get_full_classpath", return_value="/fake/cp"), \
         patch.object(jf, "get_comsol_env", return_value=os.environ.copy()), \
         patch.object(jf, "compile_stage_code",
                      return_value=MagicMock(success=True, stderr="")):
        compiled_dir = tmp_path / "java_compiled"
        compiled_dir.mkdir()
        (compiled_dir / "ModelExporter.class").write_bytes(b"\xca\xfe\xba\xbe")
        (compiled_dir / "SolverTelemetry.class").write_bytes(b"\xca\xfe\xba\xbe")
        result = generate_mph(
            builder_java=builder,
            output_mph=output,
            solve_study="std1",
            workspace_dir=tmp_path,
            run_linting=False,
        )

    assert "--solve" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--solve") + 1] == "std1"
    assert result.solved is True
    assert result.solve_study == "std1"


# ---- End-to-end (real COMSOL) ------------------------------------------

@pytest.mark.real_comsol
@pytest.mark.skipif(
    os.environ.get("COMSOL_E2E", "0") != "1",
    reason="Set COMSOL_E2E=1 to run the real-COMSOL E2E test",
)
def test_generate_mph_e2e_simplebox(tmp_path):
    """End-to-end: compile SimpleBoxBuilder, run ModelExporter, validate."""
    if not Path(COMSOL_PATH).is_dir():
        pytest.skip(f"COMSOL not installed at {COMSOL_PATH}")

    # Copy fixture into tmp_path so the workspace is isolated.
    builder = tmp_path / "SimpleBoxBuilder.java"
    shutil.copy(SIMPLE_BOX, builder)
    output = tmp_path / "SimpleBoxBuilder.mph"

    result = generate_mph(
        builder_java=builder,
        output_mph=output,
        builder_args={"size_m": "0.02"},
        workspace_dir=tmp_path,
        timeout_s=180,
    )

    assert result.success is True
    assert output.exists()
    # Structural validation again, belt-and-suspenders.
    validate_mph(output)
    # Sidecar must be present and readable.
    sidecar = Path(result.sidecar_path)
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["builder_class"] == "SimpleBoxBuilder"
    assert data["builder_args"] == {"size_m": "0.02"}
    assert data["output_sha256"] == result.output_sha256
