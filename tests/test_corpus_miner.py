"""Tests for comsol_support.corpus_miner — corpus mining pipeline."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comsol_support.corpus_miner import (
    CorpusStatistics,
    EnvironmentReport,
    ModelAnalysis,
    RE_CREATE,
    RE_MESH_CREATE,
    RE_PHYSICS,
    RE_SET,
    RE_SET_INDEX,
    RE_STUDY,
    analyze_corpus,
    generate_mph_list,
    ingest_fragments,
    mine_corpus,
    parse_java_file,
    run_batch_conversion,
    verify_environment,
)
from comsol_support.db import (
    clear_fragments_by_source,
    get_fragment,
    init_db,
    search_fragments,
    store_fragment,
)

FIXTURES = Path(__file__).parent / "fixtures" / "corpus"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """Fresh database connection."""
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def sample_java():
    return FIXTURES / "sample_model.java"


@pytest.fixture
def minimal_java():
    return FIXTURES / "minimal_model.java"


@pytest.fixture
def complex_java():
    return FIXTURES / "complex_model.java"


@pytest.fixture
def malformed_java():
    return FIXTURES / "malformed_model.java"


@pytest.fixture
def corpus_dir(tmp_path):
    """Create a corpus directory with copies of fixture files."""
    java_dir = tmp_path / "java" / "TestModule"
    java_dir.mkdir(parents=True)
    for fixture in FIXTURES.glob("*.java"):
        (java_dir / fixture.name).write_text(fixture.read_text())
    return tmp_path / "java"


# ── Schema migration tests ─────────────────────────────────────────────────


class TestSchemaMigration:
    """Test that the fragments table has a source column."""

    def test_fragments_has_source_column(self, db_conn):
        """Source column exists after init_db."""
        cols = {
            row[1]
            for row in db_conn.execute("PRAGMA table_info(fragments)")
        }
        assert "source" in cols

    def test_store_fragment_with_source(self, db_conn):
        """store_fragment accepts source parameter."""
        fid = store_fragment(
            db_conn, "geometry", "Block", "// code",
            source="corpus-6.4",
        )
        frag = get_fragment(db_conn, fid)
        assert frag["source"] == "corpus-6.4"

    def test_store_fragment_without_source(self, db_conn):
        """store_fragment works without source (backward compat)."""
        fid = store_fragment(db_conn, "geometry", "Block", "// code")
        frag = get_fragment(db_conn, fid)
        assert frag["source"] is None

    def test_clear_fragments_by_source(self, db_conn):
        """clear_fragments_by_source deletes only matching source."""
        store_fragment(db_conn, "geometry", "Block", "// a", source="corpus-6.4")
        store_fragment(db_conn, "geometry", "Sphere", "// b", source="corpus-6.4")
        store_fragment(db_conn, "mesh", "FreeTet", "// c", source="manual")

        deleted = clear_fragments_by_source(db_conn, "corpus-6.4")
        assert deleted == 2

        # Manual fragment should survive
        remaining = search_fragments(db_conn, "mesh")
        assert len(remaining) == 1
        assert remaining[0]["pattern_name"] == "FreeTet"

    def test_clear_fragments_empty_source(self, db_conn):
        """Clearing nonexistent source returns 0."""
        assert clear_fragments_by_source(db_conn, "nonexistent") == 0


# ── Regex pattern tests ────────────────────────────────────────────────────


class TestRegexPatterns:
    """Test individual regex patterns against known strings."""

    @pytest.mark.parametrize("line,expected_tag,expected_type", [
        ('.create("blk1", "Block")', "blk1", "Block"),
        ('.create("cyl1", "Cylinder")', "cyl1", "Cylinder"),
        ('.create("sph1", "Sphere")', "sph1", "Sphere"),
        ('.create("fil1", "Fillet")', "fil1", "Fillet"),
        ('.create("uni1", "Union")', "uni1", "Union"),
    ])
    def test_re_create(self, line, expected_tag, expected_type):
        match = RE_CREATE.search(line)
        assert match is not None
        assert match.group(1) == expected_tag
        assert match.group(2) == expected_type

    @pytest.mark.parametrize("line,expected_key", [
        ('.set("size", new double[]{0.1, 0.05, 0.01});', "size"),
        ('.set("r", "0.005");', "r"),
        ('.set("expr", "T");', "expr"),
        ('.set("T0", "373.15[K]");', "T0"),
    ])
    def test_re_set(self, line, expected_key):
        match = RE_SET.search(line)
        assert match is not None
        assert match.group(1) == expected_key

    def test_re_set_index(self):
        line = '.setIndex("shift", "100", 0);'
        match = RE_SET_INDEX.search(line)
        assert match is not None
        assert match.group(1) == "shift"
        assert match.group(2) == '"100"'
        assert match.group(3) == "0"

    @pytest.mark.parametrize("line,expected_type", [
        ('.physics().create("ht", "HeatTransfer"', "HeatTransfer"),
        ('.physics().create("solid", "SolidMechanics"', "SolidMechanics"),
    ])
    def test_re_physics(self, line, expected_type):
        match = RE_PHYSICS.search(line)
        assert match is not None
        assert match.group(2) == expected_type

    def test_re_study(self):
        line = '.study("std1").create("stat", "Stationary")'
        match = RE_STUDY.search(line)
        assert match is not None
        assert match.group(1) == "std1"
        assert match.group(2) == "stat"
        assert match.group(3) == "Stationary"

    def test_re_mesh_create(self):
        line = '.mesh("mesh1").create("ftet1", "FreeTet")'
        match = RE_MESH_CREATE.search(line)
        assert match is not None
        assert match.group(3) == "FreeTet"


# ── Java file parsing tests ────────────────────────────────────────────────


class TestParseJavaFile:
    """Test parse_java_file against fixture files."""

    def test_sample_model_features(self, sample_java):
        """Sample model extracts correct feature types."""
        analysis = parse_java_file(sample_java)
        assert "Block" in analysis.features
        assert "Cylinder" in analysis.features
        assert "Difference" in analysis.features
        assert analysis.features["Block"] == 1
        assert analysis.features["Cylinder"] == 1

    def test_sample_model_properties(self, sample_java):
        """Sample model extracts property keys."""
        analysis = parse_java_file(sample_java)
        assert "size" in analysis.properties
        assert "r" in analysis.properties
        assert "T0" in analysis.properties
        assert "expr" in analysis.properties

    def test_sample_model_physics(self, sample_java):
        """Sample model identifies physics types."""
        analysis = parse_java_file(sample_java)
        assert "HeatTransfer" in analysis.physics_types

    def test_sample_model_study(self, sample_java):
        """Sample model identifies study types."""
        analysis = parse_java_file(sample_java)
        assert "Stationary" in analysis.study_types

    def test_sample_model_mesh(self, sample_java):
        """Sample model identifies mesh types."""
        analysis = parse_java_file(sample_java)
        assert "FreeTet" in analysis.mesh_types

    def test_sample_model_stages(self, sample_java):
        """Sample model detects stage boundaries."""
        analysis = parse_java_file(sample_java)
        # Should have geometry, physics, mesh, studies, postprocessing stages
        assert "geometry" in analysis.stage_blocks
        assert "physics" in analysis.stage_blocks
        assert "mesh" in analysis.stage_blocks
        assert "studies" in analysis.stage_blocks
        assert "postprocessing" in analysis.stage_blocks

    def test_minimal_model(self, minimal_java):
        """Minimal model extracts only geometry."""
        analysis = parse_java_file(minimal_java)
        assert "Sphere" in analysis.features
        assert len(analysis.physics_types) == 0
        assert len(analysis.study_types) == 0
        assert "geometry" in analysis.stage_blocks

    def test_complex_model_multi_physics(self, complex_java):
        """Complex model identifies multiple physics interfaces."""
        analysis = parse_java_file(complex_java)
        assert "HeatTransfer" in analysis.physics_types
        assert "SolidMechanics" in analysis.physics_types

    def test_complex_model_multi_study(self, complex_java):
        """Complex model identifies multiple study types."""
        analysis = parse_java_file(complex_java)
        assert "Stationary" in analysis.study_types
        assert "Eigenvalue" in analysis.study_types

    def test_complex_model_boolean_geometry(self, complex_java):
        """Complex model has boolean geometry operations."""
        analysis = parse_java_file(complex_java)
        assert "Union" in analysis.features
        assert "Difference" in analysis.features
        assert "Fillet" in analysis.features

    def test_complex_model_set_index(self, complex_java):
        """Complex model extracts setIndex properties."""
        analysis = parse_java_file(complex_java)
        assert "shift" in analysis.properties

    def test_malformed_model_no_crash(self, malformed_java):
        """Malformed model doesn't crash parser."""
        analysis = parse_java_file(malformed_java)
        assert isinstance(analysis, ModelAnalysis)

    def test_malformed_model_extracts_valid_data(self, malformed_java):
        """Malformed model extracts what it can from unusual formatting."""
        analysis = parse_java_file(malformed_java)
        # The unusual .create(  "blk1"  ,  "Block"  ) with extra spaces
        # won't match RE_CREATE (which expects no extra spaces) — this is
        # acceptable since COMSOL-generated Java uses consistent formatting.
        # Properties are still extracted via .set() regex:
        assert "size" in analysis.properties
        assert "pos" in analysis.properties
        # The valid physics().create should be extracted
        assert "EmptyPhysics" in analysis.physics_types
        # Stage blocks should still be populated
        assert "geometry" in analysis.stage_blocks

    def test_nonexistent_file(self, tmp_path):
        """Nonexistent file returns empty ModelAnalysis."""
        analysis = parse_java_file(tmp_path / "nonexistent.java")
        assert analysis.features == {}
        assert analysis.properties == {}

    def test_empty_file(self, tmp_path):
        """Empty file returns empty ModelAnalysis."""
        empty = tmp_path / "empty.java"
        empty.write_text("")
        analysis = parse_java_file(empty)
        assert analysis.features == {}


# ── Corpus aggregation tests ───────────────────────────────────────────────


class TestAnalyzeCorpus:
    """Test corpus-wide aggregation."""

    def test_corpus_counts(self, corpus_dir):
        """analyze_corpus counts all models."""
        stats = analyze_corpus(corpus_dir)
        assert stats.total_models == 4  # sample, minimal, complex, malformed
        assert stats.parsed == 4
        assert stats.failed == 0

    def test_feature_frequencies(self, corpus_dir):
        """Feature frequencies aggregate across models."""
        stats = analyze_corpus(corpus_dir)
        # Block appears in sample, complex, and malformed = 3
        assert stats.feature_freq["Block"] >= 2

    def test_physics_frequencies(self, corpus_dir):
        """Physics type frequencies aggregate."""
        stats = analyze_corpus(corpus_dir)
        assert "HeatTransfer" in stats.physics_freq
        assert stats.physics_freq["HeatTransfer"] >= 2

    def test_co_occurrence_symmetric(self, corpus_dir):
        """Co-occurrence is symmetric: A→B count == B→A count."""
        stats = analyze_corpus(corpus_dir)
        for feat_a, co_map in stats.co_occurrence.items():
            for feat_b, count in co_map.items():
                assert feat_b in stats.co_occurrence, (
                    f"{feat_b} not in co_occurrence (symmetric check)"
                )
                assert stats.co_occurrence[feat_b].get(feat_a, 0) == count, (
                    f"Asymmetric: {feat_a}→{feat_b}={count} but "
                    f"{feat_b}→{feat_a}={stats.co_occurrence[feat_b].get(feat_a)}"
                )

    def test_module_distribution(self, corpus_dir):
        """Module distribution from directory structure."""
        stats = analyze_corpus(corpus_dir)
        assert "TestModule" in stats.module_distribution
        assert stats.module_distribution["TestModule"] == 4

    def test_stage_exemplars_populated(self, corpus_dir):
        """Stage exemplars are collected."""
        stats = analyze_corpus(corpus_dir)
        assert len(stats.stage_exemplars) > 0
        assert "geometry" in stats.stage_exemplars

    def test_empty_dir(self, tmp_path):
        """Empty directory produces zero-count stats."""
        empty = tmp_path / "empty"
        empty.mkdir()
        stats = analyze_corpus(empty)
        assert stats.total_models == 0
        assert stats.parsed == 0


# ── Fragment ingestion tests ───────────────────────────────────────────────


class TestIngestFragments:
    """Test Phase D fragment ingestion."""

    def _make_stats(self):
        """Create a minimal CorpusStatistics for testing."""
        stats = CorpusStatistics(
            total_models=10,
            parsed=10,
            feature_freq={
                "Block": 8,
                "Cylinder": 5,
                "Sphere": 3,
                "Rare": 1,  # Below min_freq threshold
            },
            co_occurrence={
                "Block": {"Cylinder": 4, "Sphere": 2},
                "Cylinder": {"Block": 4, "Sphere": 1},
                "Sphere": {"Block": 2, "Cylinder": 1},
            },
            stage_exemplars={
                "geometry": [
                    ("model1", 'model.geom("g1").create("blk1", "Block");'),
                    ("model2", 'model.geom("g1").create("sph1", "Sphere");'),
                ],
            },
        )
        return stats

    def test_ingest_creates_fragments(self, db_conn):
        """Fragments are created for features above threshold."""
        stats = self._make_stats()
        report = ingest_fragments(db_conn, stats, source_tag="corpus-test")
        assert report.fragments_inserted == 3  # Block, Cylinder, Sphere
        assert report.source_tag == "corpus-test"

    def test_ingest_skips_rare_features(self, db_conn):
        """Features below min_freq are skipped."""
        stats = self._make_stats()
        ingest_fragments(db_conn, stats, source_tag="corpus-test")
        # "Rare" should not be in fragments
        results = search_fragments(db_conn, "geometry", pattern_name="Rare")
        assert len(results) == 0

    def test_ingest_tier_b(self, db_conn):
        """Ingested fragments are Tier B."""
        stats = self._make_stats()
        ingest_fragments(db_conn, stats, source_tag="corpus-test")
        results = search_fragments(db_conn, "geometry")
        for frag in results:
            assert frag["tier"] == "B"

    def test_ingest_has_co_occurrence(self, db_conn):
        """Ingested fragments include co-occurrence JSON."""
        stats = self._make_stats()
        ingest_fragments(db_conn, stats, source_tag="corpus-test")
        results = search_fragments(db_conn, "geometry", pattern_name="Block")
        assert len(results) > 0
        co_json = json.loads(results[0]["co_occurrence_json"])
        assert "Cylinder" in co_json

    def test_ingest_idempotent(self, db_conn):
        """Running ingest twice produces same fragment count."""
        stats = self._make_stats()
        r1 = ingest_fragments(db_conn, stats, source_tag="corpus-test")
        r2 = ingest_fragments(db_conn, stats, source_tag="corpus-test")
        assert r1.fragments_inserted == r2.fragments_inserted
        assert r2.fragments_cleared == r1.fragments_inserted

    def test_ingest_preserves_other_sources(self, db_conn):
        """Ingesting corpus fragments doesn't delete manual fragments."""
        # Create a manual Tier A fragment
        store_fragment(db_conn, "geometry", "Manual", "// manual code",
                       source="manual", tier="A")

        stats = self._make_stats()
        ingest_fragments(db_conn, stats, source_tag="corpus-test")

        # Manual fragment should still exist
        manual = search_fragments(db_conn, "geometry", pattern_name="Manual")
        assert len(manual) == 1
        assert manual[0]["source"] == "manual"

    def test_ingest_stage_distribution(self, db_conn):
        """Ingestion report includes stage distribution."""
        stats = self._make_stats()
        report = ingest_fragments(db_conn, stats, source_tag="corpus-test")
        assert "geometry" in report.stage_distribution

    def test_ingest_corpus_freq(self, db_conn):
        """Fragments have correct corpus_freq values."""
        stats = self._make_stats()
        ingest_fragments(db_conn, stats, source_tag="corpus-test")
        results = search_fragments(db_conn, "geometry", pattern_name="Block")
        assert results[0]["corpus_freq"] == 8


# ── Exemplar selection tests ───────────────────────────────────────────────


# ── Environment verification tests (mocked) ───────────────────────────────


class TestVerifyEnvironment:
    """Test Phase A environment verification (mocked paths)."""

    def test_missing_plugins_dir(self, tmp_path):
        """Reports error when plugins dir is missing."""
        report = verify_environment(tmp_path)
        assert report.jars_found == 0
        assert any("Plugins" in e or "plugins" in e for e in report.errors)

    def test_missing_comsol_binary(self, tmp_path):
        """Reports error when COMSOL binary is missing."""
        report = verify_environment(tmp_path)
        assert not report.license_ok
        assert any("binary" in e.lower() for e in report.errors)

    def test_jar_discovery(self, tmp_path):
        """Finds all JAR files in plugins directory."""
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        (plugins / "com.comsol.model_6.4.0.jar").touch()
        (plugins / "com.comsol.model.util_6.4.0.jar").touch()
        (plugins / "com.comsol.api_1.0.0.jar").touch()
        (plugins / "org.eclipse.osgi_3.18.jar").touch()

        report = verify_environment(tmp_path)
        assert report.jars_found == 4

    def test_mph_counting(self, tmp_path):
        """Counts .mph files in applications directory."""
        apps = tmp_path / "applications" / "TestModule"
        apps.mkdir(parents=True)
        (apps / "model1.mph").touch()
        (apps / "model2.mph").touch()

        report = verify_environment(tmp_path)
        assert report.mph_count == 2

    def test_mph_zip_format(self, tmp_path):
        """Detects ZIP format from magic bytes."""
        apps = tmp_path / "applications"
        apps.mkdir()
        mph = apps / "test.mph"
        # Write ZIP magic bytes
        mph.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        report = verify_environment(tmp_path)
        assert report.mph_format == "zip"

    def test_mph_binary_format(self, tmp_path):
        """Detects binary format from non-ZIP magic bytes."""
        apps = tmp_path / "applications"
        apps.mkdir()
        mph = apps / "test.mph"
        mph.write_bytes(b"\x00\x01\x02\x03" + b"\x00" * 100)

        report = verify_environment(tmp_path)
        assert report.mph_format == "binary"

    def test_can_convert_property(self, tmp_path):
        """can_convert requires JARs and .mph files."""
        report = EnvironmentReport(jars_found=2, mph_count=10)
        assert report.can_convert

        report2 = EnvironmentReport(jars_found=0, mph_count=10)
        assert not report2.can_convert


# ── Batch conversion tests (mocked) ───────────────────────────────────────


class TestRunBatchConversion:
    """Test Phase B batch conversion with mocked subprocess."""

    def test_conversion_parses_progress(self, tmp_path):
        """Parses JSON progress output from CorpusBatchConverter."""
        mock_facade = MagicMock()
        mock_facade.find_java_executable.return_value = "/usr/bin/java"
        mock_facade.get_full_classpath.return_value = "/tmp/classes"

        mph_list = tmp_path / "mph_list.txt"
        mph_list.write_text("/path/to/model1.mph\n/path/to/model2.mph\n")

        # Mock subprocess output
        stdout = (
            '{"success":true,"mph":"/path/to/model1.mph","java":"out/model1.java","index":1,"total":2}\n'
            '{"success":false,"mph":"/path/to/model2.mph","error":"corrupt file","index":2,"total":2}\n'
            '{"done":true,"converted":1,"failed":1,"total":2}\n'
        )

        # Batch conversion now launches via Popen + communicate so the
        # JVM owns its process group (clean license release on timeout).
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (stdout, "")
        mock_proc.returncode = 0
        with patch("comsol_support.corpus_miner.subprocess.Popen",
                   return_value=mock_proc):
            report = run_batch_conversion(
                mock_facade, mph_list, tmp_path / "output"
            )

        assert report.converted == 1
        assert report.failed == 1
        assert report.total == 2

    def test_converter_is_compiled_before_it_is_run(self, tmp_path):
        """Regression: nothing else compiles CorpusBatchConverter.

        On a fresh clone the class file does not exist, so the JVM died
        with ClassNotFoundException — and because this function reads only
        stdout, that surfaced as a successful run that converted 0 models.
        """
        mock_facade = MagicMock()
        mock_facade.find_java_executable.return_value = "/usr/bin/java"
        mock_facade.get_full_classpath.return_value = "/tmp/classes"
        mock_facade.compile_comsol_class.return_value = MagicMock(
            success=True, stdout="", stderr="",
        )

        mph_list = tmp_path / "mph_list.txt"
        mph_list.write_text("/path/to/model1.mph\n")

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (
            '{"done":true,"converted":1,"failed":0,"total":1}\n', "",
        )
        mock_proc.returncode = 0
        with patch("comsol_support.corpus_miner.subprocess.Popen",
                   return_value=mock_proc):
            run_batch_conversion(mock_facade, mph_list, tmp_path / "output")

        mock_facade.compile_comsol_class.assert_called_once_with(
            "CorpusBatchConverter.java"
        )

    def test_compile_failure_reports_instead_of_running_java(self, tmp_path):
        """A failed compile must surface, not fall through to a silent run."""
        mock_facade = MagicMock()
        mock_facade.compile_comsol_class.return_value = MagicMock(
            success=False, stdout="", stderr="cannot find symbol: save",
        )

        mph_list = tmp_path / "mph_list.txt"
        mph_list.write_text("/path/to/model1.mph\n")

        with patch("comsol_support.corpus_miner.subprocess.Popen") as popen:
            report = run_batch_conversion(
                mock_facade, mph_list, tmp_path / "output"
            )

        popen.assert_not_called()
        assert report.converted == 0
        assert any("compile" in e.lower() for e in report.errors)

    def test_conversion_timeout(self, tmp_path):
        """Handles subprocess timeout gracefully and group-kills the JVM."""
        mock_facade = MagicMock()
        mock_facade.find_java_executable.return_value = "/usr/bin/java"
        mock_facade.get_full_classpath.return_value = "/tmp/classes"

        mph_list = tmp_path / "mph_list.txt"
        mph_list.write_text("/path/to/model.mph\n")

        mock_proc = MagicMock()
        # First communicate() (with timeout) raises; the post-kill
        # communicate() drains cleanly.
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired("cmd", 7200),
            ("", ""),
        ]
        mock_proc.poll.return_value = None
        with patch("comsol_support.corpus_miner.subprocess.Popen",
                   return_value=mock_proc), \
             patch("comsol_support.corpus_miner._terminate_process_group") \
                as mock_term:
            report = run_batch_conversion(
                mock_facade, mph_list, tmp_path / "output"
            )

        mock_term.assert_called_once_with(mock_proc)
        assert len(report.errors) > 0
        assert "timed out" in report.errors[0].lower()


# ── CLI dispatch tests ─────────────────────────────────────────────────────


class TestCLICorpusCommand:
    """Test CLI scrape corpus argument parsing."""

    def test_parser_accepts_corpus(self):
        """scrape corpus is a valid subcommand."""
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "scrape", "corpus",
            "--output-dir", "/tmp/corpus",
            "--skip-conversion",
        ])
        assert args.scrape_target == "corpus"
        assert args.output_dir == "/tmp/corpus"
        assert args.skip_conversion is True

    def test_parser_verify_only(self):
        """--verify-only flag is parsed."""
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "scrape", "corpus", "--verify-only",
        ])
        assert args.verify_only is True

    def test_parser_defaults(self):
        """Default values are correct."""
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "corpus"])
        assert args.version == "6.4"
        assert args.output_dir == "corpus"
        assert args.skip_conversion is False
        assert args.verify_only is False


# ── generate_mph_list tests ────────────────────────────────────────────────


class TestGenerateMphList:
    """Test mph list file generation."""

    def test_generates_list(self, tmp_path):
        """Finds .mph files and writes them to a list file."""
        apps = tmp_path / "apps"
        (apps / "mod1").mkdir(parents=True)
        (apps / "mod2").mkdir(parents=True)
        (apps / "mod1" / "a.mph").touch()
        (apps / "mod1" / "b.mph").touch()
        (apps / "mod2" / "c.mph").touch()

        output = tmp_path / "list.txt"
        count = generate_mph_list(apps, output)

        assert count == 3
        lines = output.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_empty_dir(self, tmp_path):
        """Empty directory produces count 0."""
        apps = tmp_path / "empty"
        apps.mkdir()
        output = tmp_path / "list.txt"
        count = generate_mph_list(apps, output)
        assert count == 0


# ── End-to-end pipeline tests ──────────────────────────────────────────────


class TestMineCorpus:
    """Test the full mine_corpus pipeline (skip_conversion=True)."""

    def test_skip_conversion_analyzes_existing(self, corpus_dir, db_conn, tmp_path):
        """With skip_conversion, analyzes existing .java files."""
        # corpus_dir has java/ subdirectory with fixtures
        output_dir = corpus_dir.parent
        comsol_path = tmp_path / "comsol"
        comsol_path.mkdir()

        results = mine_corpus(
            comsol_path=comsol_path,
            applications_dir=tmp_path / "apps",  # doesn't matter
            output_dir=output_dir,
            conn=db_conn,
            source_tag="corpus-test",
            skip_conversion=True,
        )

        assert "corpus_stats" in results
        assert "ingestion_report" in results
        assert results["corpus_stats"].parsed == 4
        # With only 4 fixture models, features may not reach default min_freq=3.
        # Verify the pipeline ran and produced stats.
        assert len(results["corpus_stats"].feature_freq) > 0

    def test_writes_statistics_json(self, corpus_dir, db_conn, tmp_path):
        """Pipeline writes corpus_statistics.json."""
        output_dir = corpus_dir.parent
        comsol_path = tmp_path / "comsol"
        comsol_path.mkdir()

        mine_corpus(
            comsol_path=comsol_path,
            applications_dir=tmp_path / "apps",
            output_dir=output_dir,
            conn=db_conn,
            skip_conversion=True,
        )

        stats_file = output_dir / "corpus_statistics.json"
        assert stats_file.exists()
        data = json.loads(stats_file.read_text())
        assert "feature_freq" in data
        assert "total_models" in data


# ── Java source file validation ────────────────────────────────────────────


class TestCorpusBatchConverterSource:
    """Validate CorpusBatchConverter.java source structure."""

    def test_source_exists(self):
        """CorpusBatchConverter.java exists in java/ directory."""
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        assert java_src.exists()

    def test_source_has_comsol_imports(self):
        """Source imports com.comsol.model packages."""
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert "import com.comsol.model.*;" in text
        assert "import com.comsol.model.util.*;" in text

    def test_source_has_init_standalone(self):
        """Source calls ModelUtil.initStandalone."""
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert "ModelUtil.initStandalone" in text

    def test_source_has_model_remove(self):
        """Source calls ModelUtil.remove to prevent memory leaks."""
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert "ModelUtil.remove" in text

    def test_source_has_resume_flag(self):
        """Source supports --resume flag."""
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert "--resume" in text

    def test_source_saves_in_java_format_explicitly(self):
        """Regression: save(path) writes .mph regardless of extension.

        The one-argument Model.save() has no extension-based format
        detection, so the converter silently produced .mph files named
        .java — 612 of them, from which the miner extracted zero features.
        The export format must be named: save(path, "java").
        """
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert 'model.save(javaPath, "java")' in text, (
            'converter must call save(path, "java") — the single-argument '
            "save() writes COMSOL's binary .mph format"
        )

    def test_source_exits_explicitly(self):
        """Regression: COMSOL's non-daemon threads keep the JVM alive.

        Returning from main() left the process parked forever holding a
        license seat, with the Python parent blocked until its 2-hour
        timeout. See docs/known-gotchas.md -> G-DISCONNECT-HANGS.
        """
        java_src = (
            Path(__file__).parent.parent
            / "comsol_support" / "java" / "CorpusBatchConverter.java"
        )
        text = java_src.read_text()
        assert "System.exit(" in text or "halt(" in text, (
            "converter must terminate the JVM explicitly — via System.exit() "
            "or Runtime.halt() — or it never terminates"
        )
