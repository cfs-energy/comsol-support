"""Tests for the Reference Manual property-key scraper."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from comsol_support.refmanual_scraper import (
    PropertyRecord,
    ScrapeResult,
    check_pdftotext,
    classify_feature_stage,
    extract_pdf_text,
    ingest_properties,
    parse_property_tables,
    scrape_from_text,
    scrape_reference_manual,
    stage_distribution,
    _extract_feature_from_table_title,
    _detect_columns,
)
from comsol_support.db import (
    init_db,
    search_knowledge,
    store_knowledge_batch,
)


FIXTURES = Path(__file__).parent / "fixtures" / "refmanual"


# ── Fixture helpers ────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ── TestPdfTextExtraction ──────────────────────────────────────────────────


class TestPdfTextExtraction:
    """Test PDF text extraction via pdftotext subprocess."""

    def test_check_pdftotext_found(self):
        with patch("shutil.which", return_value="/usr/bin/pdftotext"):
            assert check_pdftotext() == "/usr/bin/pdftotext"

    def test_check_pdftotext_missing(self):
        with patch("shutil.which", return_value=None):
            assert check_pdftotext() is None

    def test_extract_raises_when_pdftotext_missing(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_text("fake")
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="pdftotext.*required"):
                extract_pdf_text(pdf)

    def test_extract_raises_for_missing_pdf(self, tmp_path):
        pdf = tmp_path / "nonexistent.pdf"
        with patch("shutil.which", return_value="/usr/bin/pdftotext"):
            with pytest.raises(FileNotFoundError, match="PDF not found"):
                extract_pdf_text(pdf)

    def test_extract_calls_pdftotext_correctly(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_text("fake")
        mock_result = MagicMock(returncode=0, stdout="extracted text", stderr="")
        with patch("shutil.which", return_value="/usr/bin/pdftotext"):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                result = extract_pdf_text(pdf)
                assert result == "extracted text"
                args = mock_run.call_args[0][0]
                assert args[0] == "/usr/bin/pdftotext"
                assert "-layout" in args
                assert str(pdf) in args
                assert "-" in args  # stdout output
                assert mock_run.call_args[1]["timeout"] == 120

    def test_extract_raises_on_pdftotext_failure(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_text("fake")
        mock_result = MagicMock(returncode=1, stderr="conversion error")
        with patch("shutil.which", return_value="/usr/bin/pdftotext"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(RuntimeError, match="pdftotext failed"):
                    extract_pdf_text(pdf)


# ── TestFeatureNameExtraction ──────────────────────────────────────────────


class TestFeatureNameExtraction:
    """Test feature name extraction from table titles."""

    @pytest.mark.parametrize("title,expected", [
        ("VALID PROPERTY/VALUE PAIRS FOR BLOCK.", "Block"),
        ("VALID PROPERTY/VALUE PAIRS FOR CYLINDER.", "Cylinder"),
        ("PROPERTIES FOR STATIONARY.", "Stationary"),
        ("PROPERTY FOR ROTATINGBOUNDARY.", "Rotatingboundary"),
        ("VALID GENERAL PROPERTY/VALUE PAIRS FOR ADVANCED.", "Advanced"),
        ("VALID EIGENVALUE PROPERTIES.", "Eigenvalue"),
        ("VALID FULLY COUPLED PROPERTIES.", "Coupled"),
        ("INTERPOLATION PROPERTIES.", "Interpolation"),
        ("SURROGATEMODELTRAINING PROPERTIES", "Surrogatemodeltraining"),
        ("VALID COMBINESOLUTION PROPERTIES.", "Combinesolution"),
        # Noise words should not be extracted
        ("GENERAL PROPERTIES.", ""),
        ("VALID PROPERTIES.", ""),
    ])
    def test_extract_feature_name(self, title, expected):
        result = _extract_feature_from_table_title(title)
        assert result == expected


# ── TestColumnDetection ────────────────────────────────────────────────────


class TestColumnDetection:

    def test_four_columns(self):
        header = "PROPERTY                 VALUE                   DEFAULT     DESCRIPTION"
        cols = _detect_columns(header)
        assert len(cols) == 4
        assert cols[0] == 0  # PROPERTY

    def test_three_columns(self):
        header = "NAME            TYPE           DESCRIPTION"
        cols = _detect_columns(header)
        assert len(cols) == 3


# ── TestPropertyTableParsing ───────────────────────────────────────────────


class TestPropertyTableParsing:
    """Test parsing of property tables from fixture text."""

    def test_block_fixture(self):
        text = _load_fixture("block_properties.txt")
        records = parse_property_tables(text)
        assert len(records) == 7
        keys = {r.property_key for r in records}
        assert "size" in keys
        assert "pos" in keys
        assert "base" in keys
        assert "axis" in keys
        assert "rot" in keys
        assert "type" in keys
        assert "axistype" in keys

    def test_block_feature_name(self):
        text = _load_fixture("block_properties.txt")
        records = parse_property_tables(text)
        # All should have feature_name "Block" from table title
        for r in records:
            assert r.feature_name == "Block", f"Expected Block, got {r.feature_name} for {r.property_key}"

    def test_block_value_types(self):
        text = _load_fixture("block_properties.txt")
        records = parse_property_tables(text)
        by_key = {r.property_key: r for r in records}
        assert "double[]" in by_key["size"].value_type
        assert "corner | center" in by_key["base"].value_type
        assert "double" == by_key["rot"].value_type

    def test_block_defaults(self):
        text = _load_fixture("block_properties.txt")
        records = parse_property_tables(text)
        by_key = {r.property_key: r for r in records}
        assert by_key["size"].default_value == "{1,1,1}"
        assert by_key["pos"].default_value == "{0,0,0}"
        assert by_key["rot"].default_value == "0"

    def test_block_table_id(self):
        text = _load_fixture("block_properties.txt")
        records = parse_property_tables(text)
        assert all(r.table_id == "3-31" for r in records)

    def test_multi_section_splits_features(self):
        text = _load_fixture("multi_section.txt")
        records = parse_property_tables(text)
        features = {r.feature_name for r in records}
        assert "Cylinder" in features
        assert "Sphere" in features
        assert "Cone" in features

    def test_multi_section_cylinder_properties(self):
        text = _load_fixture("multi_section.txt")
        records = parse_property_tables(text)
        cyl_records = [r for r in records if r.feature_name == "Cylinder"]
        assert len(cyl_records) == 6
        keys = {r.property_key for r in cyl_records}
        assert "r" in keys
        assert "h" in keys
        assert "ang" in keys

    def test_multi_section_total_count(self):
        text = _load_fixture("multi_section.txt")
        records = parse_property_tables(text)
        assert len(records) == 16  # 6 + 4 + 6

    def test_solver_fixture(self):
        text = _load_fixture("solver_properties.txt")
        records = parse_property_tables(text)
        assert len(records) == 10
        features = {r.feature_name for r in records}
        assert "Stationary" in features
        assert "Eigenvalue" in features

    def test_solver_stationary_properties(self):
        text = _load_fixture("solver_properties.txt")
        records = parse_property_tables(text)
        stat = [r for r in records if r.feature_name == "Stationary"]
        keys = {r.property_key for r in stat}
        assert "reltol" in keys
        assert "maxiter" in keys
        assert "damp" in keys

    def test_malformed_table_resilience(self):
        text = _load_fixture("malformed_table.txt")
        records = parse_property_tables(text)
        # Should extract goodprop and finalprop, skip continuation text
        keys = {r.property_key for r in records}
        assert "goodprop" in keys
        assert "finalprop" in keys
        # Should NOT extract continuation text as properties
        assert "this" not in keys
        assert "and" not in keys

    def test_malformed_extracts_valid_rows_only(self):
        text = _load_fixture("malformed_table.txt")
        records = parse_property_tables(text)
        assert len(records) == 2


# ── TestStageClassification ────────────────────────────────────────────────


class TestStageClassification:

    @pytest.mark.parametrize("feature,expected", [
        ("Block", "geometry"),
        ("Cylinder", "geometry"),
        ("Sphere", "geometry"),
        ("Stationary", "studies"),
        ("Eigenvalue", "studies"),
        ("Material", "materials"),
        ("FreeTri", "mesh"),
        ("Size", "mesh"),
        ("Interpolation", "functions"),
        ("Table", "postprocessing"),
        ("UnknownFeature", None),
    ])
    def test_classify_feature_stage(self, feature, expected):
        assert classify_feature_stage(feature) == expected

    def test_prefix_fallback(self):
        """Javadoc STAGE_MAP_PREFIX is used as fallback."""
        # "GeomSomething" should match Geom→geometry via prefix
        assert classify_feature_stage("GeomNewFeature") == "geometry"
        assert classify_feature_stage("SolverNewFeature") == "studies"
        assert classify_feature_stage("ResultNewFeature") == "postprocessing"


# ── TestKnowledgeIngestion ─────────────────────────────────────────────────


class TestKnowledgeIngestion:

    def test_ingest_populates_knowledge(self, db):
        records = [
            PropertyRecord("Block", "size", "double[]", "{1,1,1}",
                          "Edge lengths.", "3-31", "geometry"),
            PropertyRecord("Block", "pos", "double[]", "{0,0,0}",
                          "Position.", "3-31", "geometry"),
        ]
        count = ingest_properties(db, records)
        assert count == 2

    def test_ingest_sets_correct_fields(self, db):
        records = [
            PropertyRecord("Block", "size", "double[]", "{1,1,1}",
                          "Edge lengths.", "3-31", "geometry"),
        ]
        ingest_properties(db, records)
        row = db.execute(
            "SELECT * FROM knowledge WHERE property_key = 'size'"
        ).fetchone()
        assert row is not None
        assert row["class"] == "Block"
        assert row["method"] == "set"
        assert row["property_key"] == "size"
        assert row["value_type"] == "double[]"
        assert row["stage"] == "geometry"
        assert row["source"] == "refmanual-6.4"
        assert "Edge lengths" in row["description"]
        assert "[default: {1,1,1}]" in row["description"]

    def test_ingest_signature_format(self, db):
        records = [
            PropertyRecord("Block", "size", "double[]", "{1,1,1}",
                          "Edge lengths.", "3-31", "geometry"),
        ]
        ingest_properties(db, records)
        row = db.execute(
            "SELECT signature FROM knowledge WHERE property_key = 'size'"
        ).fetchone()
        assert row["signature"] == 'set("size", value)'

    def test_ingest_fts_searchable(self, db):
        records = [
            PropertyRecord("Block", "size", "double[]", "{1,1,1}",
                          "Edge lengths.", "3-31", "geometry"),
        ]
        ingest_properties(db, records)
        results = search_knowledge(db, "size")
        assert len(results) >= 1
        assert results[0]["property_key"] == "size"

    def test_ingest_with_stage_filter(self, db):
        records = [
            PropertyRecord("Block", "size", "double[]", "{1,1,1}",
                          "Edge lengths.", "3-31", "geometry"),
            PropertyRecord("Stationary", "reltol", "positive double", "1e-6",
                          "Relative tolerance.", "6-10", "studies"),
        ]
        ingest_properties(db, records)
        geom = search_knowledge(db, "size", stage="geometry")
        assert len(geom) >= 1
        study = search_knowledge(db, "reltol", stage="studies")
        assert len(study) >= 1


# ── TestIdempotency ────────────────────────────────────────────────────────


class TestIdempotency:

    def test_double_scrape_same_count(self, db):
        text = _load_fixture("block_properties.txt")
        r1 = scrape_from_text(text, db)
        r2 = scrape_from_text(text, db)
        assert r1.rows_inserted == r2.rows_inserted
        # Check actual row count in DB
        count = db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE source = 'refmanual-6.4'"
        ).fetchone()[0]
        assert count == r1.rows_inserted

    def test_idempotent_source_isolation(self, db):
        """Javadoc and RefManual rows coexist independently."""
        # Insert some javadoc rows
        store_knowledge_batch(db, [
            ("GeomSequence", "create", "create()", None, "GeomFeature",
             "geometry", None, "javadoc-6.4", "Creates a geometry feature"),
        ])
        javadoc_count = db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE source = 'javadoc-6.4'"
        ).fetchone()[0]
        assert javadoc_count == 1

        # Now scrape refmanual
        text = _load_fixture("block_properties.txt")
        scrape_from_text(text, db)

        # Javadoc rows should still be there
        javadoc_count_after = db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE source = 'javadoc-6.4'"
        ).fetchone()[0]
        assert javadoc_count_after == 1

        # RefManual rows should be present
        refmanual_count = db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE source = 'refmanual-6.4'"
        ).fetchone()[0]
        assert refmanual_count == 7


# ── TestCrossReference ─────────────────────────────────────────────────────


class TestCrossReference:

    def test_fts_finds_both_sources(self, db):
        """FTS search returns results from both Javadoc and RefManual."""
        # Insert javadoc row for Block
        store_knowledge_batch(db, [
            ("GeomSequence", "create", "create(String, String)", None,
             "GeomFeature", "geometry", None, "javadoc-6.4",
             "Creates a geometry feature like Block"),
        ])
        # Insert refmanual rows for Block
        text = _load_fixture("block_properties.txt")
        scrape_from_text(text, db)

        # Search for "Block" should find both
        results = search_knowledge(db, "Block", stage="geometry")
        sources = {r["source"] for r in results}
        assert "javadoc-6.4" in sources
        assert "refmanual-6.4" in sources


# ── TestScrapeResult ───────────────────────────────────────────────────────


class TestScrapeResult:

    def test_scrape_from_text_result(self, db):
        text = _load_fixture("multi_section.txt")
        result = scrape_from_text(text, db)
        assert result.tables_found == 3  # 3 separate tables
        assert result.records_extracted == 16
        assert result.rows_inserted == 16

    def test_scrape_result_errors(self):
        result = ScrapeResult()
        result.errors.append("test error")
        assert len(result.errors) == 1


# ── TestStageDistribution ─────────────────────────────────────────────────


class TestStageDistribution:

    def test_distribution_from_fixture(self, db):
        text = _load_fixture("multi_section.txt")
        scrape_from_text(text, db)
        dist = stage_distribution(db)
        assert "geometry" in dist
        assert dist["geometry"] == 16  # All are geometry features


# ── TestCLIRefmanualCommand ────────────────────────────────────────────────


class TestCLIRefmanualCommand:

    def test_scrape_subcommand_with_refmanual(self):
        """Test that scrape refmanual subcommand is wired in CLI."""
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "refmanual", "--db", "/tmp/test.db"])
        assert args.command == "scrape"
        assert args.scrape_target == "refmanual"
        assert args.db == "/tmp/test.db"

    def test_scrape_javadoc_still_works(self):
        """Backward compat: scrape without target defaults to javadoc."""
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "javadoc", "--db", "/tmp/test.db"])
        assert args.scrape_target == "javadoc"

    def test_scrape_refmanual_pdf_arg(self):
        from comsol_support.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["scrape", "refmanual", "--pdf", "/some/path.pdf"])
        assert args.pdf == "/some/path.pdf"


# ── Integration test (requires real PDF + pdftotext) ───────────────────────


from comsol_support import COMSOL_PATH

_REAL_PDF = (
    Path(COMSOL_PATH) / "doc" / "pdf" / "COMSOL_Multiphysics"
    / "COMSOL_ProgrammingReferenceManual.pdf"
)


@pytest.mark.skipif(
    not _REAL_PDF.exists() or not check_pdftotext(),
    reason="Requires COMSOL installation and pdftotext",
)
class TestRealPDFIntegration:

    def test_scrape_real_pdf(self, db):
        result = scrape_reference_manual(_REAL_PDF, db)
        assert result.records_extracted > 500
        assert result.rows_inserted > 500
        assert result.tables_found > 50

    def test_real_pdf_known_properties(self, db):
        scrape_reference_manual(_REAL_PDF, db)
        # Block.size should exist
        results = search_knowledge(db, "size", stage="geometry")
        assert any(r["property_key"] == "size" for r in results)

    def test_real_pdf_stage_distribution(self, db):
        scrape_reference_manual(_REAL_PDF, db)
        dist = stage_distribution(db)
        assert "geometry" in dist
        assert "studies" in dist
        assert dist["geometry"] > 100
        assert dist["studies"] > 100

    def test_real_pdf_fts_search(self, db):
        scrape_reference_manual(_REAL_PDF, db)
        results = search_knowledge(db, "reltol")
        assert len(results) > 0
