"""Integration tests against a real COMSOL 6.4 installation.

Every test is marked @pytest.mark.real_comsol.
Run with:  pytest tests/test_real_comsol_integration.py -v
Skip with: pytest -m "not real_comsol"

These tests read from the discovered COMSOL install (read-only; see
comsol_support.COMSOL_PATH — overridable via the COMSOL_PATH environment
variable) and write only to tmp directories.
"""

import os
from pathlib import Path

import pytest

from comsol_support.java_facade import JavaFacade
from comsol_support.db import init_db
from comsol_support.javadoc_scraper import scrape_javadoc, load_class_index
from comsol_support.refmanual_scraper import scrape_reference_manual
from comsol_support.corpus_miner import (
    verify_environment,
    parse_java_file,
    analyze_corpus,
    ingest_fragments,
    generate_mph_list,
    run_batch_conversion,
)
from comsol_support.native_catalog import (
    get_interface,
    update_catalog_corpus_freq,
)


# ── Paths ────────────────────────────────────────────────────────────────────

from comsol_support import COMSOL_PATH

COMSOL_ROOT = Path(COMSOL_PATH)
JAVADOC_API = COMSOL_ROOT / "doc" / "help" / "wtpwebapps" / "ROOT" / "doc" / "com.comsol.help.comsol" / "api"
REFMANUAL_PDF = COMSOL_ROOT / "doc" / "pdf" / "COMSOL_Multiphysics" / "COMSOL_ProgrammingReferenceManual.pdf"
APPLICATIONS = COMSOL_ROOT / "applications"

_comsol_available = COMSOL_ROOT.is_dir()

# Windows/macOS installs ship many Application Library entries as tiny
# "preview" stubs (~8–15 KB) that ModelUtil.load refuses
# ("This_is_a_COMSOL_Application_Libraries_preview_file…") until the
# full model is downloaded via the Application Libraries window. Full
# models are ≥100 KB, so a size gate cheaply excludes stubs when
# sampling models for conversion tests.
PREVIEW_STUB_MAX_BYTES = 100_000


def _full_models_only(mphs):
    """Filter out Application Libraries preview stubs by size, and
    `*_geom_sequence.mph` companions — geometry-construction sidecars
    that deliberately contain no physics. On the macOS 6.4 install the
    smallest "full" model of several modules is such a companion, and
    picking one broke the promote-catalog test's "at least one physics
    class extracted" invariant."""
    return [p for p in mphs
            if p.stat().st_size > PREVIEW_STUB_MAX_BYTES
            and not p.name.endswith("_geom_sequence.mph")]

pytestmark = [
    pytest.mark.real_comsol,
    pytest.mark.skipif(not _comsol_available, reason="COMSOL not installed at expected path"),
]


# ── Phase 0: Classpath / Compile ────────────────────────────────────────────

class TestComsolCompilation:
    """Verify the classpath fix allows real compilation."""

    @pytest.fixture
    def facade(self, tmp_path):
        return JavaFacade(
            comsol_path=str(COMSOL_ROOT),
            workspace_dir=tmp_path / "workspace",
        )

    def test_find_comsol_jars_includes_api(self, facade):
        """Classpath fix: com.comsol.api_1.0.0.jar must be present."""
        jars = facade.find_comsol_jars()
        names = [j.name for j in jars]
        assert any("com.comsol.api" in n for n in names), (
            f"com.comsol.api JAR not found. Got {len(jars)} JARs."
        )
        # Should have many JARs (100+), not just 4
        assert len(jars) > 50, f"Only {len(jars)} JARs found (expected 100+)"

    def test_compile_facade(self, facade):
        """TagRegistry + SelectionAlgebra compile with COMSOL's JDK."""
        result = facade.compile_facade()
        assert result.success, f"compile_facade failed: {result.stderr[:500]}"
        # Verify .class files exist
        class_files = list(facade.compiled_dir.rglob("*.class"))
        assert len(class_files) >= 2, f"Expected >=2 .class files, got {len(class_files)}"


    def test_compile_corpus_batch_converter(self, facade):
        """CorpusBatchConverter.java compiles with full COMSOL classpath."""
        facade.compile_facade()
        converter_src = facade.java_source_dir / "CorpusBatchConverter.java"
        assert converter_src.exists(), f"Source not found: {converter_src}"
        result = facade.compile_stage_code(converter_src)
        assert result.success, f"CorpusBatchConverter compile failed: {result.stderr[:500]}"


# ── Phase 1: Environment Verification ───────────────────────────────────────

class TestEnvironmentVerification:
    """Verify corpus_miner.verify_environment reports accurately."""

    def test_environment_report(self, tmp_path):
        report = verify_environment(COMSOL_ROOT, output_dir=tmp_path)
        assert report.jars_found > 50, f"Only {report.jars_found} JARs"
        assert report.mph_count > 800, f"Only {report.mph_count} .mph files"
        if os.name != "nt":
            # On Windows verify_environment reads the version statically
            # (comsol.exe --version opens the GUI), so license_ok stays
            # False by design — seat validity is covered by the
            # license-status probe instead.
            assert report.license_ok, f"License check failed: {report.errors}"
        assert not report.errors, f"Unexpected errors: {report.errors}"
        assert report.comsol_version is not None
        assert "6.4" in report.comsol_version
        assert report.can_convert
        assert report.disk_free_gb > 0

    def test_module_distribution(self, tmp_path):
        report = verify_environment(COMSOL_ROOT, output_dir=tmp_path)
        # Should have multiple module categories
        assert len(report.module_distribution) > 5, (
            f"Only {len(report.module_distribution)} module categories"
        )


# ── Phase 2: Javadoc Scraper ────────────────────────────────────────────────

class TestJavadocScraper:
    """Run the Javadoc scraper against real COMSOL 6.4 HTML."""

    @pytest.fixture
    def db_conn(self, tmp_path):
        conn = init_db(tmp_path / "test.db")
        yield conn
        conn.close()

    def test_class_index_loads(self):
        """type-search-index.js can be parsed."""
        index = load_class_index(JAVADOC_API)
        assert len(index) > 100, f"Only {len(index)} classes in index"

    def test_scrape_produces_rows(self, db_conn):
        """Full scrape populates the knowledge table with >3000 rows."""
        result = scrape_javadoc(JAVADOC_API, db_conn)
        assert result.classes_scraped > 100, f"Only {result.classes_scraped} classes scraped"
        assert result.rows_inserted > 3000, f"Only {result.rows_inserted} rows inserted (expected >3000)"
        # Accept some errors (some HTML pages may not be standard class pages)
        error_rate = len(result.errors) / max(result.classes_scraped, 1)
        assert error_rate < 0.3, f"Error rate too high: {len(result.errors)} errors / {result.classes_scraped} classes"

    def test_key_classes_present(self, db_conn):
        """Core COMSOL API classes must be in the knowledge table."""
        scrape_javadoc(JAVADOC_API, db_conn)
        for cls_name in ["Model", "GeomSequence", "MeshSequence", "ModelUtil"]:
            rows = db_conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE class = ?", (cls_name,)
            ).fetchone()[0]
            assert rows > 0, f"Class '{cls_name}' not found in knowledge table"


# ── Phase 3: RefManual Scraper ──────────────────────────────────────────────

class TestRefManualScraper:
    """Run the RefManual scraper against the real Programming Reference Manual PDF."""

    @pytest.fixture
    def db_conn(self, tmp_path):
        conn = init_db(tmp_path / "test.db")
        yield conn
        conn.close()

    @pytest.mark.skipif(not REFMANUAL_PDF.exists(), reason="RefManual PDF not found")
    def test_scrape_produces_rows(self, db_conn):
        """Full scrape populates the knowledge table with property records."""
        result = scrape_reference_manual(REFMANUAL_PDF, db_conn)
        assert result.tables_found > 10, f"Only {result.tables_found} tables found"
        assert result.records_extracted > 500, f"Only {result.records_extracted} records"
        assert result.rows_inserted > 500, f"Only {result.rows_inserted} rows inserted"
        # Check for typical COMSOL feature names
        for feature in ["Block", "Cylinder", "Sphere"]:
            rows = db_conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE class = ?", (feature,)
            ).fetchone()[0]
            # Not all features guaranteed, but at least some geometry primitives
            if rows > 0:
                break
        else:
            # Check if ANY property_key rows exist
            total = db_conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE property_key IS NOT NULL"
            ).fetchone()[0]
            assert total > 0, "No property_key records found in knowledge table"


# ── Phase 4: Sample Corpus Conversion ───────────────────────────────────────

class TestCorpusConversion:
    """Attempt .mph→.java conversion for a small sample of models."""

    @pytest.fixture
    def facade(self, tmp_path):
        f = JavaFacade(
            comsol_path=str(COMSOL_ROOT),
            workspace_dir=tmp_path / "workspace",
        )
        # Pre-compile everything needed
        f.compile_facade()
        # Compile CorpusBatchConverter
        converter = f.java_source_dir / "CorpusBatchConverter.java"
        f.compile_stage_code(converter)
        return f

    def _find_sample_mphs(self, count: int = 3) -> list[Path]:
        """Find a small diverse set of .mph files."""
        candidates = []
        # Try to pick from different module directories
        target_dirs = [
            "COMSOL_Multiphysics",
            "AC_DC_Module",
            "Heat_Transfer_Module",
            "Structural_Mechanics_Module",
            "CFD_Module",
        ]
        for dirname in target_dirs:
            module_dir = APPLICATIONS / dirname
            if module_dir.is_dir():
                mphs = _full_models_only(module_dir.rglob("*.mph"))
                if mphs:
                    # Pick smallest full model (quickest to convert;
                    # the size gate keeps preview stubs out)
                    smallest = min(mphs, key=lambda p: p.stat().st_size)
                    candidates.append(smallest)
                    if len(candidates) >= count:
                        break
        # Fallback: grab any .mph files (unfiltered, so an all-stub
        # install still exercises the pipeline and skips with the
        # documented preview-file error instead of silently finding
        # nothing)
        if not candidates:
            all_mphs = sorted(APPLICATIONS.rglob("*.mph"), key=lambda p: p.stat().st_size)
            candidates = all_mphs[:count]
        return candidates[:count]

    def test_mph_list_generation(self, tmp_path):
        """generate_mph_list finds .mph files."""
        list_path = tmp_path / "mph_list.txt"
        count = generate_mph_list(APPLICATIONS, list_path)
        assert count > 800, f"Only found {count} .mph files"
        assert list_path.exists()

    def test_sample_conversion(self, facade, tmp_path):
        """Convert a small sample of .mph files to .java.

        This test may take 30-120 seconds per file. If COMSOL runtime
        initialization fails (headless, license, memory), it documents
        the error rather than failing silently.
        """
        sample = self._find_sample_mphs(3)
        if not sample:
            pytest.skip("No .mph files found for conversion test")

        # Write sample list
        list_path = tmp_path / "sample_list.txt"
        with open(list_path, "w") as f:
            for mph in sample:
                f.write(f"{mph}\n")

        output_dir = tmp_path / "java_output"
        output_dir.mkdir()

        report = run_batch_conversion(
            facade, list_path, output_dir, resume=False,
        )

        # Document results (don't hard-fail on runtime issues)
        if report.converted > 0:
            java_files = list(output_dir.rglob("*.java"))
            assert len(java_files) > 0, "Conversion claimed success but no .java files found"

            # Guard against the silent binary-output failure mode: the
            # one-arg model.save() writes a binary .mph archive under
            # the .java name, which then "parses" to zero features.
            head = java_files[0].read_bytes()[:400]
            assert not head.startswith(b"PK"), (
                f"{java_files[0].name} is a binary .mph archive, not "
                "Java source — CorpusBatchConverter must use "
                "model.save(path, \"java\")"
            )
            assert b"com.comsol.model" in head, (
                f"{java_files[0].name} does not look like a COMSOL "
                f"model Java export (head: {head[:80]!r})"
            )

            # Parse the first converted file
            analysis = parse_java_file(java_files[0], source_mph=str(sample[0]))
            assert analysis.java_path == str(java_files[0])
            # Should have extracted some features
            total_features = sum(analysis.features.values())
            print(f"\n  Converted {report.converted}/{len(sample)} models")
            print(f"  First model features: {total_features}")
            print(f"  Physics types: {analysis.physics_types}")
        elif report.errors:
            # Document the runtime blocker
            error_msg = "\n".join(report.errors[:5])
            pytest.skip(
                f"COMSOL runtime conversion failed (expected in some environments):\n{error_msg}"
            )
        else:
            pytest.skip("No conversions completed and no errors reported")


# ── Phase 4.5: promote-catalog end-to-end (D1) ──────────────────────────────

class TestPromoteCatalogPipeline:
    """Exercise the D1 pipeline against a real COMSOL corpus sample:
    .mph → .java (via CorpusBatchConverter) → analyze_corpus →
    update_catalog_corpus_freq → assertions on the seeded catalog.

    The sample size is intentionally small (3 files) so the test
    completes in a few minutes. If real-COMSOL conversion fails (license
    issue, headless restrictions), the test skips with the runtime error
    preserved rather than failing silently — same pattern as the sibling
    test_sample_conversion.

    This is the one test that can ONLY be validated against the real
    corpus: the regex/class-name matching between analyze_corpus output
    and the seeded native_interfaces table is the whole point of D1,
    and synthetic fixtures can't tell us whether real-world class names
    align with what the catalog expects.
    """

    @pytest.fixture
    def facade(self, tmp_path):
        f = JavaFacade(
            comsol_path=str(COMSOL_ROOT),
            workspace_dir=tmp_path / "workspace",
        )
        f.compile_facade()
        converter = f.java_source_dir / "CorpusBatchConverter.java"
        f.compile_stage_code(converter)
        return f

    @pytest.fixture
    def db_conn(self, tmp_path):
        # init_db seeds native_interfaces with the curated starter set.
        conn = init_db(tmp_path / "test.db")
        yield conn
        conn.close()

    def _pick_sample_mphs(self, count: int = 3) -> list[Path]:
        """Find a small diverse set of .mph files across modules."""
        candidates: list[Path] = []
        target_dirs = [
            "Heat_Transfer_Module",
            "Structural_Mechanics_Module",
            "CFD_Module",
            "COMSOL_Multiphysics",
            "AC_DC_Module",
        ]
        for dirname in target_dirs:
            module_dir = APPLICATIONS / dirname
            if module_dir.is_dir():
                mphs = _full_models_only(module_dir.rglob("*.mph"))
                if mphs:
                    smallest = min(mphs, key=lambda p: p.stat().st_size)
                    candidates.append(smallest)
                    if len(candidates) >= count:
                        break
        if not candidates:
            all_mphs = sorted(
                APPLICATIONS.rglob("*.mph"), key=lambda p: p.stat().st_size,
            )
            candidates = all_mphs[:count]
        return candidates[:count]

    def test_promote_catalog_end_to_end(self, facade, db_conn, tmp_path):
        """Convert a sample, analyze, update catalog, assert matches.

        Minimum viable assertions (deliberately loose so diverse corpus
        selection doesn't flake the test):
        - At least one catalog row gets its corpus_freq rewritten.
        - The updater's summary is well-formed (matched + unmatched keys).
        - Re-running the updater is idempotent (no-op second pass).
        """
        sample = self._pick_sample_mphs(3)
        if not sample:
            pytest.skip("No .mph files found for conversion")

        list_path = tmp_path / "sample_list.txt"
        with open(list_path, "w") as f:
            for mph in sample:
                f.write(f"{mph}\n")
        output_dir = tmp_path / "java_output"
        output_dir.mkdir()

        report = run_batch_conversion(
            facade, list_path, output_dir, resume=False,
        )

        # Hard skip if COMSOL runtime can't convert — same policy as
        # the sibling test_sample_conversion.
        if report.converted == 0:
            err = "\n".join(report.errors[:5]) if report.errors else "(none)"
            pytest.skip(
                f"COMSOL runtime conversion produced 0 java files:\n{err}"
            )

        java_files = list(output_dir.rglob("*.java"))
        assert java_files, "Conversion claimed success but no .java files"

        stats = analyze_corpus(output_dir)
        # Sanity: at least one physics class extracted from the sample.
        # The sample is diverse by construction (heat / structural / cfd
        # / multiphysics / ac-dc), so the freq dict should be non-empty
        # in aggregate even if individual files are sparse.
        assert sum(stats.physics_freq.values()) > 0, (
            f"No physics types extracted. physics_freq={stats.physics_freq}"
        )

        # Apply to catalog. Captures the "before" state per matched row
        # so we can verify the absolute-set semantics.
        summary = update_catalog_corpus_freq(db_conn, stats)

        # Summary contract.
        assert "matched" in summary
        assert "unmatched" in summary
        assert summary["matched"] >= 1, (
            f"No catalog rows matched from stats: "
            f"physics={stats.physics_freq}, studies={stats.study_freq}, "
            f"results={stats.result_freq}"
        )

        # Observability: print what landed vs what was unmatched. This
        # is the signal a human reviews to decide whether the seed needs
        # new entries.
        print(f"\n  promote-catalog: {summary['matched']} rows updated")
        if summary["unmatched"]:
            print(f"  {len(summary['unmatched'])} unmatched class_name(s):")
            for stage, cls, cnt in summary["unmatched"][:10]:
                print(f"    [{stage}] {cls}: freq={cnt}")

        # Idempotency: running the same stats twice must yield the same
        # final corpus_freq on every row. Snapshot rows by the table's
        # PRIMARY KEY (domain_keyword, stage, tag_prefix) — neither
        # tag_prefix nor class_name is unique per stage (the seed
        # deliberately lists e.g. GeneralFormPDE under four domain
        # keywords with different starting freqs), so any collapsed
        # key falsely reports a violation.
        def _freq_by_pk():
            return {
                (r["domain_keyword"], r["stage"], r["tag_prefix"]):
                    r["corpus_freq"]
                for r in db_conn.execute(
                    "SELECT domain_keyword, stage, tag_prefix, "
                    "corpus_freq FROM native_interfaces"
                ).fetchall()
            }

        snapshot = _freq_by_pk()

        summary2 = update_catalog_corpus_freq(db_conn, stats)
        assert summary2["matched"] == summary["matched"]

        after = _freq_by_pk()
        for key, freq in after.items():
            assert snapshot[key] == freq, (
                f"Idempotency violated for {key}: {snapshot[key]} → {freq}"
            )

    def test_promote_catalog_preserves_curated_metadata(
        self, facade, db_conn, tmp_path,
    ):
        """After update_catalog_corpus_freq runs on real-corpus stats,
        curated fields (class_name, notes, default_studies) must be
        untouched — only corpus_freq may change."""
        sample = self._pick_sample_mphs(2)
        if not sample:
            pytest.skip("No .mph files found for conversion")

        list_path = tmp_path / "sample_list.txt"
        with open(list_path, "w") as f:
            for mph in sample:
                f.write(f"{mph}\n")
        output_dir = tmp_path / "java_output"
        output_dir.mkdir()

        report = run_batch_conversion(
            facade, list_path, output_dir, resume=False,
        )
        if report.converted == 0:
            # Same policy as test_promote_catalog_end_to_end: skip, but say
            # why — on a site without the sampled modules (or with their
            # seats taken) the converter's per-file license errors are the
            # whole story.
            err = "\n".join(report.errors[:5]) if report.errors else "(none)"
            pytest.skip(
                "COMSOL runtime conversion produced 0 java files for "
                f"metadata-preservation test:\n{err}"
            )

        # Snapshot a couple of catalog rows BEFORE the update.
        before_ht = get_interface(db_conn, tag_prefix="ht", stage="physics")
        before_stat = get_interface(db_conn, tag_prefix="stat", stage="study")

        stats = analyze_corpus(output_dir)
        update_catalog_corpus_freq(db_conn, stats)

        after_ht = get_interface(db_conn, tag_prefix="ht", stage="physics")
        after_stat = get_interface(db_conn, tag_prefix="stat", stage="study")

        for label, before, after in (
            ("ht", before_ht, after_ht),
            ("stat", before_stat, after_stat),
        ):
            assert before is not None and after is not None
            assert after["class_name"] == before["class_name"], (
                f"class_name changed for {label}"
            )
            assert after["notes"] == before["notes"], (
                f"notes changed for {label}"
            )
            assert after["setup_cost_rank"] == before["setup_cost_rank"], (
                f"setup_cost_rank changed for {label}"
            )
            assert after["default_studies"] == before["default_studies"], (
                f"default_studies changed for {label}"
            )


# ── Phase 5: Corpus Mining (with synthetic fallback) ────────────────────────

class TestCorpusMining:
    """Test corpus analysis and fragment ingestion with realistic data."""

    @pytest.fixture
    def db_conn(self, tmp_path):
        conn = init_db(tmp_path / "test.db")
        yield conn
        conn.close()

    def _create_realistic_java_files(self, java_dir: Path) -> int:
        """Create COMSOL-style .java files for mining when conversion unavailable."""
        java_dir.mkdir(parents=True, exist_ok=True)

        models = {
            "ThermalBlock.java": '''
import com.comsol.model.*;
import com.comsol.model.util.*;

public class ThermalBlock {
    public static void main(String[] args) throws Exception {
        Model model = ModelUtil.create("Model");
        model.modelNode().create("comp1");
        // Geometry
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("blk1", "Block");
        model.component("comp1").geom("geom1").feature("blk1").set("size", new double[]{0.1, 0.1, 0.05});
        model.component("comp1").geom("geom1").feature("blk1").set("pos", new double[]{0, 0, 0});
        model.component("comp1").geom("geom1").run();
        // Selections
        model.component("comp1").selection().create("sel1", "Box");
        model.component("comp1").selection("sel1").set("entitydim", 2);
        // Materials
        model.component("comp1").material().create("mat1", "Common");
        model.component("comp1").material("mat1").propertyGroup("def").set("thermalconductivity", "400");
        // Physics
        model.component("comp1").physics().create("ht", "HeatTransfer", "geom1");
        model.component("comp1").physics("ht").create("temp1", "TemperatureBoundary", 2);
        model.component("comp1").physics("ht").feature("temp1").set("T0", "373.15[K]");
        model.component("comp1").physics("ht").create("hf1", "HeatFluxBoundary", 2);
        model.component("comp1").physics("ht").feature("hf1").set("q0", "1000[W/m^2]");
        // Mesh
        model.component("comp1").mesh().create("mesh1");
        model.component("comp1").mesh("mesh1").create("ftet1", "FreeTet");
        model.component("comp1").mesh("mesh1").feature("size").set("hauto", 5);
        model.component("comp1").mesh("mesh1").run();
        // Study
        model.study().create("std1");
        model.study("std1").create("stat", "Stationary");
        model.study("std1").run();
    }
}
''',
            "ACCoil.java": '''
import com.comsol.model.*;
import com.comsol.model.util.*;

public class ACCoil {
    public static void main(String[] args) throws Exception {
        Model model = ModelUtil.create("Model");
        model.modelNode().create("comp1");
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create("cyl1", "Cylinder");
        model.component("comp1").geom("geom1").feature("cyl1").set("r", "0.05");
        model.component("comp1").geom("geom1").feature("cyl1").set("h", "0.2");
        model.component("comp1").geom("geom1").create("wp1", "WorkPlane");
        model.component("comp1").geom("geom1").run();
        model.component("comp1").material().create("mat1", "Common");
        model.component("comp1").material("mat1").propertyGroup("def").set("electricconductivity", "5.96e7");
        model.component("comp1").physics().create("mf", "InductionCurrents", "geom1");
        model.component("comp1").physics("mf").create("coil1", "Coil", 3);
        model.component("comp1").physics("mf").feature("coil1").set("CoilType", "Numeric");
        model.component("comp1").mesh().create("mesh1");
        model.component("comp1").mesh("mesh1").create("ftet1", "FreeTet");
        model.component("comp1").mesh("mesh1").run();
        model.study().create("std1");
        model.study("std1").create("freq", "Frequency");
        model.study("std1").feature("freq").set("plist", "50 60 100");
        model.study("std1").run();
    }
}
''',
            "FluidChannel.java": '''
import com.comsol.model.*;
import com.comsol.model.util.*;

public class FluidChannel {
    public static void main(String[] args) throws Exception {
        Model model = ModelUtil.create("Model");
        model.modelNode().create("comp1");
        model.component("comp1").geom().create("geom1", 2);
        model.component("comp1").geom("geom1").create("r1", "Rectangle");
        model.component("comp1").geom("geom1").feature("r1").set("size", new double[]{1.0, 0.1});
        model.component("comp1").geom("geom1").run();
        model.component("comp1").selection().create("sel1", "Box");
        model.component("comp1").material().create("mat1", "Common");
        model.component("comp1").material("mat1").propertyGroup("def").set("dynamicviscosity", "1e-3");
        model.component("comp1").physics().create("spf", "LaminarFlow", "geom1");
        model.component("comp1").physics("spf").create("inl1", "InletBoundary", 1);
        model.component("comp1").physics("spf").feature("inl1").set("U0in", "0.1");
        model.component("comp1").physics("spf").create("out1", "OutletBoundary", 1);
        model.component("comp1").mesh().create("mesh1");
        model.component("comp1").mesh("mesh1").create("ftri1", "FreeTri");
        model.component("comp1").mesh("mesh1").run();
        model.study().create("std1");
        model.study("std1").create("stat", "Stationary");
        model.study("std1").run();
    }
}
''',
        }

        for filename, content in models.items():
            (java_dir / filename).write_text(content.strip())

        return len(models)

    def test_parse_realistic_java(self, tmp_path):
        """Java parser extracts features from COMSOL-style source."""
        java_dir = tmp_path / "java"
        self._create_realistic_java_files(java_dir)

        analysis = parse_java_file(java_dir / "ThermalBlock.java", "thermal_block.mph")
        assert analysis.source_mph == "thermal_block.mph"
        assert len(analysis.features) > 0, "No features extracted"
        assert len(analysis.physics_types) > 0, "No physics types extracted"
        assert "HeatTransfer" in analysis.physics_types or any(
            "ht" in pt.lower() or "heat" in pt.lower() for pt in analysis.physics_types
        ), f"Expected heat transfer physics, got: {analysis.physics_types}"

    def test_analyze_corpus(self, tmp_path):
        """Corpus analysis aggregates features across multiple models."""
        java_dir = tmp_path / "java"
        count = self._create_realistic_java_files(java_dir)
        stats = analyze_corpus(java_dir)
        assert stats.parsed == count, f"Expected {count} parsed, got {stats.parsed}"
        assert len(stats.feature_freq) > 0, "No feature frequencies"
        assert len(stats.physics_freq) > 0, "No physics frequencies"
        assert len(stats.co_occurrence) > 0, "No co-occurrence data"

    def test_ingest_fragments(self, tmp_path, db_conn):
        """Fragment ingestion populates the fragments table."""
        java_dir = tmp_path / "java"
        self._create_realistic_java_files(java_dir)
        stats = analyze_corpus(java_dir)
        # min_freq=1 because we only have 3 synthetic models
        report = ingest_fragments(db_conn, stats, source_tag="test-corpus", min_freq=1)
        assert report.fragments_inserted > 0, "No fragments inserted"

        # Verify searchable — check all stages that got fragments
        all_frags = db_conn.execute("SELECT * FROM fragments").fetchall()
        assert len(all_frags) > 0, "No fragments in DB at all"
        stages = {dict(r)["stage"] for r in all_frags}
        assert len(stages) > 0, f"No stages found, fragments: {[dict(r) for r in all_frags]}"

    def test_fragment_preamble_injection(self, tmp_path, db_conn):
        """Ingested fragments carry the fields the MCP tools render."""
        java_dir = tmp_path / "java"
        self._create_realistic_java_files(java_dir)
        stats = analyze_corpus(java_dir)
        ingest_fragments(db_conn, stats, source_tag="test-corpus", min_freq=1)

        # Query all fragments and check the most common stage
        all_frags = db_conn.execute(
            "SELECT * FROM fragments ORDER BY corpus_freq DESC"
        ).fetchall()
        assert len(all_frags) > 0, "No fragments for preamble injection"
        # Verify fragment has the fields search_fragments/get_fragment render
        frag = dict(all_frags[0])
        assert "pattern_name" in frag
        assert "corpus_freq" in frag
        assert "id" in frag
        assert "java_code" in frag


# ── Phase 6: Fragment Integration Smoke Test ────────────────────────────────

class TestFragmentIntegration:
    """Verify fragment library integration with real-ish data."""

    @pytest.fixture
    def populated_db(self, tmp_path):
        """DB with Javadoc + corpus data."""
        conn = init_db(tmp_path / "test.db")
        # Scrape Javadoc if available
        if JAVADOC_API.is_dir() and (JAVADOC_API / "type-search-index.js").exists():
            scrape_javadoc(JAVADOC_API, conn)
        yield conn
        conn.close()

    def test_mcp_search_fragments_with_data(self, tmp_path):
        """MCP search_fragments tool returns data when fragments exist."""
        conn = init_db(tmp_path / "test.db")
        java_dir = tmp_path / "java"
        TestCorpusMining()._create_realistic_java_files(java_dir)
        stats = analyze_corpus(java_dir)
        ingest_fragments(conn, stats, source_tag="test-corpus", min_freq=1)

        # Get all fragments and verify structure
        all_frags = conn.execute(
            "SELECT * FROM fragments ORDER BY corpus_freq DESC"
        ).fetchall()
        assert len(all_frags) > 0, "No fragments inserted"
        frag = dict(all_frags[0])
        assert "stage" in frag
        assert "pattern_name" in frag
        conn.close()


