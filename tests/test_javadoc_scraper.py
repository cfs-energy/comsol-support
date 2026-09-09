"""Tests for comsol_support.javadoc_scraper — Javadoc ontology extraction."""

from pathlib import Path

import pytest

from comsol_support.db import (
    clear_knowledge_by_source,
    init_db,
    search_knowledge,
    store_knowledge_batch,
    store_knowledge_row,
)
from comsol_support.javadoc_scraper import (
    _clean_text,
    _parse_method_signature,
    classify_stage,
    load_class_index,
    parse_class_page,
    scrape_javadoc,
    stage_distribution,
)

FIXTURES = Path(__file__).parent / "fixtures" / "javadoc"

from comsol_support import COMSOL_PATH

REAL_API_DIR = (
    Path(COMSOL_PATH) / "doc" / "help" / "wtpwebapps" / "ROOT"
    / "doc" / "com.comsol.help.comsol" / "api"
)


# ── ClassIndexParser ──────────────────────────────────────────────────────────


class TestClassIndexParser:
    def test_parse_fixture_index(self):
        results = load_class_index(FIXTURES)
        names = [r["name"] for r in results]
        assert "GeomSequence" in names
        assert "ModelNode" in names
        assert "SolverFeature" in names
        assert "Physics" in names

    def test_skips_non_class_entries(self):
        results = load_class_index(FIXTURES)
        names = [r["name"] for r in results]
        assert "All Classes" not in names

    def test_package_extraction(self):
        results = load_class_index(FIXTURES)
        by_name = {r["name"]: r for r in results}
        assert by_name["GeomSequence"]["package"] == "com.comsol.model"
        assert by_name["Physics"]["package"] == "com.comsol.model.physics"

    def test_rel_path_construction(self):
        results = load_class_index(FIXTURES)
        by_name = {r["name"]: r for r in results}
        assert by_name["GeomSequence"]["rel_path"] == "com/comsol/model/GeomSequence.html"
        assert by_name["Physics"]["rel_path"] == "com/comsol/model/physics/Physics.html"

    def test_missing_index_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_class_index(tmp_path)

    def test_malformed_index_raises(self, tmp_path):
        (tmp_path / "type-search-index.js").write_text("not valid json stuff")
        with pytest.raises(ValueError, match="No JSON array"):
            load_class_index(tmp_path)


# ── ClassPageParser ───────────────────────────────────────────────────────────


class TestClassPageParser:
    def test_geom_sequence_class_name(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        assert record is not None
        assert record.name == "GeomSequence"

    def test_geom_sequence_kind(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        assert record.kind == "interface"

    def test_geom_sequence_description(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        assert "Geometry sequence" in record.description

    def test_geom_sequence_superinterfaces(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        assert "GeomContainer" in record.superinterfaces
        assert "ModelEntity" in record.superinterfaces

    def test_geom_sequence_method_count(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        assert len(record.methods) == 6

    def test_geom_sequence_method_details(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        by_sig = {m.name: m for m in record.methods}

        create = by_sig.get("create")
        assert create is not None
        assert "GeomFeature" in create.return_type
        assert "tag" in create.signature
        assert "Creates a geometry feature" in create.description

    def test_geom_sequence_overloaded_method(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        abs_methods = [m for m in record.methods if m.name == "absRepairTol"]
        assert len(abs_methods) == 2
        return_types = {m.return_type for m in abs_methods}
        assert "double" in return_types
        assert "void" in return_types

    def test_deprecated_method(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        argument = next((m for m in record.methods if m.name == "argument"), None)
        assert argument is not None
        assert argument.deprecated is True

    def test_non_deprecated_methods(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/GeomSequence.html")
        create = next((m for m in record.methods if m.name == "create"), None)
        assert create is not None
        assert create.deprecated is False

    def test_model_node_superinterfaces(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/ModelNode.html")
        assert "AbstractModel" in record.superinterfaces
        assert "ModelEntity" in record.superinterfaces
        assert "PrimitiveModelEntity" in record.superinterfaces

    def test_model_node_description(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/ModelNode.html")
        assert "Model node" in record.description

    def test_solver_feature_overloads(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/SolverFeature.html")
        set_methods = [m for m in record.methods if m.name == "set"]
        assert len(set_methods) == 2

    def test_physics_methods(self):
        record = parse_class_page(FIXTURES / "com/comsol/model/physics/Physics.html")
        assert record.name == "Physics"
        assert len(record.methods) == 3
        create = next((m for m in record.methods if m.name == "create"), None)
        assert create is not None
        assert "PhysicsFeature" in create.return_type

    def test_missing_file_returns_none(self):
        result = parse_class_page(Path("/nonexistent/path.html"))
        assert result is None


# ── StageClassification ───────────────────────────────────────────────────────


class TestStageClassification:
    def test_geom_prefix(self):
        assert classify_stage("GeomSequence", "com.comsol.model") == "geometry"
        assert classify_stage("GeomFeature", "com.comsol.model") == "geometry"

    def test_mesh_prefix(self):
        assert classify_stage("MeshSequence", "com.comsol.model") == "mesh"
        assert classify_stage("MeshFeature", "com.comsol.model") == "mesh"

    def test_solver_prefix(self):
        assert classify_stage("SolverFeature", "com.comsol.model") == "studies"
        assert classify_stage("SolverSequence", "com.comsol.model") == "studies"

    def test_study_prefix(self):
        assert classify_stage("StudyFeature", "com.comsol.model") == "studies"

    def test_result_prefix(self):
        assert classify_stage("ResultFeature", "com.comsol.model") == "postprocessing"
        assert classify_stage("NumericalFeature", "com.comsol.model") == "postprocessing"

    def test_material_prefix(self):
        assert classify_stage("MaterialModel", "com.comsol.model") == "materials"

    def test_selection_prefix(self):
        assert classify_stage("SelectionFeature", "com.comsol.model") == "selections"

    def test_param_prefix(self):
        assert classify_stage("ParamBase", "com.comsol.model") == "parameters"
        assert classify_stage("ModelParam", "com.comsol.model") == "parameters"

    def test_function_prefix(self):
        assert classify_stage("FunctionFeature", "com.comsol.model") == "functions"

    def test_physics_package(self):
        assert classify_stage("Physics", "com.comsol.model.physics") == "physics"
        assert classify_stage("PhysicsFeature", "com.comsol.model.physics") == "physics"

    def test_untagged_class(self):
        assert classify_stage("Model", "com.comsol.model") is None
        assert classify_stage("ModelEntity", "com.comsol.model") is None

    def test_database_api_untagged(self):
        assert classify_stage("Database", "com.comsol.api.database") is None


# ── KnowledgeIngestion ────────────────────────────────────────────────────────


class TestKnowledgeIngestion:
    @pytest.fixture
    def db(self, tmp_path):
        return init_db(tmp_path / "test.db")

    def test_store_and_search(self, db):
        store_knowledge_row(
            db, "GeomSequence",
            method="create", signature="create(String tag, String type)",
            value_type="GeomFeature", stage="geometry",
            source="javadoc-6.4", description="Creates a geometry feature",
        )
        results = search_knowledge(db, "geometry create")
        assert len(results) >= 1
        assert results[0]["class"] == "GeomSequence"

    def test_batch_insert(self, db):
        rows = [
            ("GeomSequence", "create", "create(String, String)", None,
             "GeomFeature", "geometry", None, "javadoc-6.4", "Creates feature"),
            ("GeomSequence", "run", "run()", None,
             "void", "geometry", None, "javadoc-6.4", "Runs sequence"),
        ]
        count = store_knowledge_batch(db, rows)
        assert count == 2
        results = search_knowledge(db, "GeomSequence")
        assert len(results) >= 2

    def test_clear_by_source(self, db):
        rows = [
            ("A", "m1", "m1()", None, "void", None, None, "javadoc-6.4", "desc"),
            ("B", "m2", "m2()", None, "void", None, None, "other-source", "desc"),
        ]
        store_knowledge_batch(db, rows)
        deleted = clear_knowledge_by_source(db, "javadoc-6.4")
        assert deleted == 1
        remaining = db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        assert remaining == 1

    def test_stage_filtered_search(self, db):
        rows = [
            ("GeomSeq", "create", "create()", None, "void", "geometry", None, "javadoc-6.4", "geo"),
            ("MeshSeq", "create", "create()", None, "void", "mesh", None, "javadoc-6.4", "mesh"),
        ]
        store_knowledge_batch(db, rows)
        geo_results = search_knowledge(db, "create", stage="geometry")
        assert all(r["stage"] == "geometry" for r in geo_results)


# ── Idempotency ───────────────────────────────────────────────────────────────


class TestIdempotency:
    @pytest.fixture
    def db(self, tmp_path):
        return init_db(tmp_path / "test.db")

    def test_scrape_twice_same_count(self, db):
        r1 = scrape_javadoc(FIXTURES, db, source_tag="test-fixture")
        count1 = db.execute("SELECT COUNT(*) FROM knowledge WHERE source='test-fixture'").fetchone()[0]

        r2 = scrape_javadoc(FIXTURES, db, source_tag="test-fixture")
        count2 = db.execute("SELECT COUNT(*) FROM knowledge WHERE source='test-fixture'").fetchone()[0]

        assert count1 == count2
        assert r1.rows_inserted == r2.rows_inserted


# ── ScrapeResult ──────────────────────────────────────────────────────────────


class TestScrapeResult:
    @pytest.fixture
    def db(self, tmp_path):
        return init_db(tmp_path / "test.db")

    def test_fixture_scrape_counts(self, db):
        result = scrape_javadoc(FIXTURES, db, source_tag="test-fixture")
        assert result.classes_scraped == 4
        assert result.methods_extracted > 0
        assert result.rows_inserted > 0
        assert result.rows_inserted == result.classes_scraped + result.methods_extracted

    def test_fixture_scrape_errors_empty(self, db):
        result = scrape_javadoc(FIXTURES, db, source_tag="test-fixture")
        assert len(result.errors) == 0

    def test_missing_html_recorded_as_error(self, db, tmp_path):
        index_path = tmp_path / "type-search-index.js"
        index_path.write_text(
            'typeSearchIndex = [{"p":"com.comsol.model","l":"Missing"}]'
        )
        result = scrape_javadoc(tmp_path, db, source_tag="test-missing")
        assert len(result.errors) == 1
        assert "Missing" in result.errors[0]

    def test_stage_distribution(self, db):
        scrape_javadoc(FIXTURES, db, source_tag="test-fixture")
        dist = stage_distribution(db, "test-fixture")
        assert "geometry" in dist
        assert "studies" in dist
        assert "physics" in dist


# ── CLIScrapeCommand ──────────────────────────────────────────────────────────


class TestCLIScrapeCommand:
    def test_parser_has_scrape(self):
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "javadoc", "--api-dir", "/tmp/api", "--version", "6.3"])
        assert args.command == "scrape"
        assert args.scrape_target == "javadoc"
        assert args.api_dir == "/tmp/api"
        assert args.version == "6.3"

    def test_parser_scrape_defaults(self):
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "javadoc"])
        assert args.command == "scrape"
        assert args.scrape_target == "javadoc"
        assert args.api_dir is None
        assert args.version == "6.4"

    def test_scrape_dispatch_exists(self):
        from comsol_support.cli import cmd_scrape
        assert callable(cmd_scrape)


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_clean_text_whitespace(self):
        assert _clean_text("  hello   world  ") == "hello world"

    def test_clean_text_zero_width_space(self):
        assert _clean_text("hello\u200bworld") == "helloworld"

    def test_parse_method_signature_simple(self):
        name, sig = _parse_method_signature("run()")
        assert name == "run"
        assert sig == "run()"

    def test_parse_method_signature_with_params(self):
        name, sig = _parse_method_signature("create (String tag, String type)")
        assert name == "create"
        assert "String tag" in sig

    def test_parse_method_signature_empty(self):
        name, sig = _parse_method_signature("")
        assert name == ""
        assert sig == ""


# ── Integration (requires real COMSOL install) ────────────────────────────────


@pytest.mark.skipif(
    not REAL_API_DIR.exists(),
    reason="COMSOL Javadoc not available"
)
class TestRealJavadocIntegration:
    @pytest.fixture
    def db(self, tmp_path):
        return init_db(tmp_path / "test.db")

    def test_real_index_count(self):
        results = load_class_index(REAL_API_DIR)
        assert len(results) >= 440

    def test_real_scrape(self, db):
        result = scrape_javadoc(REAL_API_DIR, db)
        assert result.classes_scraped >= 400
        assert result.methods_extracted >= 2000
        assert result.rows_inserted >= 3000

    def test_real_fts_search(self, db):
        scrape_javadoc(REAL_API_DIR, db)
        results = search_knowledge(db, "GeomSequence")
        assert len(results) >= 1
        assert any(r["class"] == "GeomSequence" for r in results)

    def test_real_stage_distribution(self, db):
        scrape_javadoc(REAL_API_DIR, db)
        dist = stage_distribution(db)
        assert "geometry" in dist
        assert "physics" in dist
        assert "mesh" in dist
        assert dist["geometry"] > 50
