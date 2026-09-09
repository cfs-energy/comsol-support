"""CLI entry point for the deterministic ``comsol-support`` verbs.

Every subcommand here is a deterministic tool (knowledge scrapers, model
generation/editing/querying, linting, telemetry, doctor); the calling agent
owns planning. The LLM build orchestrator that used to live behind ``build`` /
``resume`` / ``status`` / ``list`` was retired in 2026-08 — see
``docs/modeling-practice.md`` for the modeling knowledge it carried.
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from comsol_support import COMSOL_PATH, __version__
from comsol_support.config import Config
from comsol_support.doctor import add_doctor_subparser, cmd_doctor
from comsol_support.db import init_db, search_knowledge
from comsol_support.edit_mph import add_editmph_subparser, cmd_editmph
from comsol_support.gotcha_search import (
    add_gotcha_search_subparser, cmd_gotcha_search,
)
from comsol_support.linting import add_check_subparser, cmd_check
from comsol_support.mphgen import add_mphgen_subparser, cmd_mphgen
from comsol_support.knowledge_format import format_knowledge_row
from comsol_support.license_status import (
    add_license_status_subparser, cmd_license_status,
)
from comsol_support.query_mph import add_query_mph_subparser, cmd_query_mph
from comsol_support.run_harness import add_run_harness_subparser, cmd_run_harness
from comsol_support.telemetry import (
    add_ingest_telemetry_subparser, cmd_ingest_telemetry,
)

logger = logging.getLogger("comsol_support")



def _setup_logging(verbose: bool = False) -> None:
    """Configure structured logging with timestamps and stage context."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")


def cmd_scrape(args: argparse.Namespace) -> int:
    """Scrape COMSOL docs into the knowledge table."""
    target = getattr(args, "scrape_target", None)

    if not target:
        print("Usage: comsol-support scrape {javadoc,refmanual,corpus}")
        print("  javadoc    Scrape Javadoc API docs")
        print("  refmanual  Scrape Reference Manual PDF")
        print("  corpus     Mine .mph application models into fragment table")
        return 1

    if target == "refmanual":
        return _scrape_refmanual(args)
    elif target == "corpus":
        return _scrape_corpus(args)
    elif target == "slots":
        return _scrape_slots(args)
    else:
        return _scrape_javadoc(args)


def _scrape_javadoc(args: argparse.Namespace) -> int:
    """Scrape Javadoc API docs into the knowledge table."""
    from comsol_support.javadoc_scraper import scrape_javadoc, stage_distribution

    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)

    api_dir = Path(args.api_dir) if getattr(args, "api_dir", None) else (
        Path(COMSOL_PATH) / "doc" / "help" / "wtpwebapps" / "ROOT"
        / "doc" / "com.comsol.help.comsol" / "api"
    )
    if not api_dir.exists():
        print(f"API directory not found: {api_dir}")
        return 1

    source_tag = f"javadoc-{args.version}"
    conn = init_db(config.db_path)

    print(f"Scraping COMSOL {args.version} Javadoc from: {api_dir}")
    result = scrape_javadoc(api_dir, conn, source_tag=source_tag)

    print(f"Scraped {result.classes_scraped} classes, "
          f"{result.methods_extracted} methods")
    print(f"Inserted {result.rows_inserted} rows into knowledge table")

    dist = stage_distribution(conn, source_tag)
    if dist:
        print("Stage distribution:")
        for stage, count in dist.items():
            print(f"  {stage}: {count}")

    if result.errors:
        print(f"Parse errors: {len(result.errors)}")
        for err in result.errors[:10]:
            print(f"  {err}")

    conn.close()
    return 0


def _scrape_refmanual(args: argparse.Namespace) -> int:
    """Scrape Reference Manual PDF into the knowledge table."""
    from comsol_support.refmanual_scraper import (
        scrape_reference_manual, stage_distribution, check_pdftotext,
    )

    if not check_pdftotext():
        print("Error: pdftotext (Poppler) is required and must be on PATH.")
        print("  Linux:   apt install poppler-utils")
        print("  macOS:   brew install poppler")
        print("  Windows: winget install oschwartz10612.Poppler "
              "(or: conda install -c conda-forge poppler)")
        return 1

    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)

    pdf_path = Path(args.pdf) if getattr(args, "pdf", None) else (
        Path(COMSOL_PATH) / "doc" / "pdf" / "COMSOL_Multiphysics"
        / "COMSOL_ProgrammingReferenceManual.pdf"
    )
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return 1

    source_tag = f"refmanual-{args.version}"
    conn = init_db(config.db_path)

    print(f"Scraping COMSOL {args.version} Reference Manual from: {pdf_path}")
    result = scrape_reference_manual(pdf_path, conn, source_tag=source_tag)

    print(f"Found {result.tables_found} tables, "
          f"extracted {result.records_extracted} property records")
    print(f"Inserted {result.rows_inserted} rows into knowledge table")

    dist = stage_distribution(conn, source_tag)
    if dist:
        print("Stage distribution:")
        for stage, count in dist.items():
            print(f"  {stage}: {count}")

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors[:10]:
            print(f"  {err}")

    conn.close()
    return 0


def _scrape_corpus(args: argparse.Namespace) -> int:
    """Mine COMSOL application models into the fragment table."""
    from comsol_support.corpus_miner import mine_corpus, verify_environment

    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)

    comsol_path = Path(
        getattr(args, "comsol_path", None) or COMSOL_PATH
    )

    applications_dir = Path(args.applications_dir) if getattr(
        args, "applications_dir", None
    ) else comsol_path / "applications"

    output_dir = Path(args.output_dir)
    source_tag = f"corpus-{args.version}"
    conn = init_db(config.db_path)

    # If verify-only, just run Phase A
    if getattr(args, "verify_only", False):
        report = verify_environment(comsol_path, applications_dir, output_dir=output_dir)
        print(f"COMSOL version: {report.comsol_version or 'unknown'}")
        print(f"License: {'OK' if report.license_ok else 'not verified'}")
        print(f"JARs found: {report.jars_found}")
        print(f".mph files: {report.mph_count}")
        print(f".mph format: {report.mph_format}")
        print(f"Pre-existing .java files: {report.java_files_found}")
        print(f"Disk free: {report.disk_free_gb:.1f} GB")
        if report.module_distribution:
            print("Module distribution:")
            for mod, count in sorted(
                report.module_distribution.items(), key=lambda x: -x[1]
            ):
                print(f"  {mod}: {count}")
        if report.errors:
            print(f"Errors: {len(report.errors)}")
            for err in report.errors:
                print(f"  {err}")
        conn.close()
        return 0

    # Build JavaFacade if conversion is needed
    java_facade = None
    if not args.skip_conversion:
        from comsol_support.java_facade import JavaFacade
        java_facade = JavaFacade(
            comsol_path=str(comsol_path),
            workspace_dir=str(output_dir),
        )

    print(f"Mining COMSOL {args.version} application corpus")
    print(f"  Applications: {applications_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Skip conversion: {args.skip_conversion}")

    results = mine_corpus(
        comsol_path=comsol_path,
        applications_dir=applications_dir,
        output_dir=output_dir,
        conn=conn,
        source_tag=source_tag,
        skip_conversion=args.skip_conversion,
        java_facade=java_facade,
    )

    # Report results
    # Conversion outcomes first — a run that converts nothing still reaches
    # the analysis print below, so without this the pipeline reports
    # "0 models parsed" with no indication of why.
    if "conversion_report" in results:
        conv = results["conversion_report"]
        print(f"\nConversion: {conv.converted} converted, "
              f"{conv.failed} failed, {conv.total} total")
        if conv.errors:
            from collections import Counter

            def _classify(err: str) -> str:
                if "Could_not_obtain_license" in err:
                    return "missing module license"
                if "preview_file" in err:
                    return "Application Library preview stub (not downloaded)"
                return err.strip()[:80]

            print(f"Failure reasons ({len(conv.errors)} total):")
            for reason, count in Counter(
                _classify(e) for e in conv.errors
            ).most_common(10):
                print(f"  {count:5}  {reason}")

    if "corpus_stats" in results:
        stats = results["corpus_stats"]
        print(f"\nCorpus analysis: {stats.parsed} models parsed, "
              f"{len(stats.feature_freq)} unique features")
        print("Top features:")
        for feat, freq in sorted(
            stats.feature_freq.items(), key=lambda x: -x[1]
        )[:15]:
            print(f"  {feat}: {freq}")

    if "ingestion_report" in results:
        ing = results["ingestion_report"]
        print(f"\nFragment ingestion: {ing.fragments_inserted} inserted "
              f"({ing.fragments_cleared} cleared)")
        if ing.stage_distribution:
            print("Stage distribution:")
            for stage, count in sorted(ing.stage_distribution.items()):
                print(f"  {stage}: {count}")

    conn.close()
    return 0


def _scrape_slots(args: argparse.Namespace) -> int:
    """Harvest physics-feature slots from corpus .mph files (Phase 1)
    and, unless --harvest-only, aggregate into the SQLite catalog
    (Phase 2).
    """
    from comsol_support.slot_harvest import collect_mph_paths, run_harvest

    comsol_path = Path(
        getattr(args, "comsol_path", None) or COMSOL_PATH
    )
    applications_dir = Path(args.applications_dir) if getattr(
        args, "applications_dir", None
    ) else comsol_path / "applications"

    output = Path(args.output)
    workspace = Path(getattr(args, "workspace", None) or "slot_harvest_workspace")

    # Phase 1 — harvest, unless --aggregate-only.
    if not getattr(args, "aggregate_only", False):
        mph_paths = collect_mph_paths(
            applications_dir,
            max_models=getattr(args, "max_models", None),
        )
        print(
            f"Found {len(mph_paths)} .mph files under {applications_dir}"
        )
        if not mph_paths:
            print("Nothing to harvest.")
            return 1

        print(f"Appending JSONL records to {output}")

        report = run_harvest(
            mph_paths, output,
            comsol_path=comsol_path,
            workspace_dir=workspace,
            timeout_s=getattr(args, "timeout", 7200),
            progress=True,
            version=getattr(args, "version", None),
        )

        print(
            f"\nModels processed: {report.models_processed}/"
            f"{report.mph_count}"
        )
        print(f"Slots emitted: {report.slots_emitted}")
        print(f"Variable unit declarations: {report.var_units_emitted}")
        print(f"Per-model errors: {report.errors}")
        if report.error_lines:
            print(
                f"stderr lines: {len(report.error_lines)} (first 5 shown)"
            )
            for line in report.error_lines[:5]:
                print(f"  {line}")

    # Phase 2 — aggregate into SQLite catalog, unless --harvest-only.
    if getattr(args, "harvest_only", False):
        return 0

    if not output.exists():
        print(f"Dump {output} missing; cannot aggregate.")
        return 1

    from comsol_support.slot_catalog import aggregate_dump

    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)
    conn = init_db(config.db_path)

    print(f"\nAggregating {output} → {config.db_path}")
    agg = aggregate_dump(
        output, conn,
        clear_existing=getattr(args, "clear_catalog", False),
    )
    conn.close()

    print(
        f"Slots read: {agg.slots_read}   analyzed: {agg.slots_analyzed}"
    )
    print(
        f"  resolved: {agg.slots_resolved}"
        f"   unresolved: {agg.slots_unresolved}"
        f"   translation_error: {agg.slots_translation_error}"
    )
    print(
        f"Catalog keys: {agg.catalog_keys}   "
        f"high: {agg.catalog_high}   medium: {agg.catalog_medium}   "
        f"low: {agg.catalog_low}"
    )
    print(
        f"Source-B records: {agg.var_unit_records}   "
        f"keys: {agg.var_unit_keys}"
    )
    if agg.errors:
        print(f"Aggregation errors: {len(agg.errors)} (first 5 shown)")
        for e in agg.errors[:5]:
            print(f"  {e}")
    return 0


def cmd_slot_stats(args: argparse.Namespace) -> int:
    """Report on the slot expected-unit catalog (Phase 4).

    Three views (select with --show-scattered / --show-high-confidence /
    --show-coverage). Each view is a separate query; output is tabular
    by default or JSON with --json.
    """
    from comsol_support.slot_catalog import (
        high_confidence_entries, low_coverage_keys, scattered_keys,
        suggest_thresholds,
    )
    from comsol_support.db import count_slot_catalog

    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)

    conn = init_db(config.db_path)

    if getattr(args, "suggest_thresholds", False):
        result = suggest_thresholds(
            conn,
            target_high_count=getattr(args, "target_high", 50),
            target_medium_count=getattr(args, "target_medium", 200),
        )
        import json as _json
        if args.json:
            print(_json.dumps(result, indent=2))
        else:
            print("=== Threshold suggestion ===")
            print(f"Observed: {result['observed']}")
            print(f"Current:  {result['current']}")
            print(f"Suggested: {result['suggested']}")
            print("Rationale:")
            for line in result["rationale"]:
                print(f"  {line}")
            print()
            print(
                "To apply, edit the constants at the top of "
                "comsol_support/slot_catalog.py and re-run `scrape slots "
                "--aggregate-only` (no re-harvest needed)."
            )
        conn.close()
        return 0

    if args.show_high_confidence:
        rows = high_confidence_entries(conn)
        view = "high_confidence"
    elif args.show_coverage:
        rows = low_coverage_keys(conn)
        view = "low_coverage"
    elif args.show_scattered:
        rows = scattered_keys(conn)
        view = "scattered"
    else:
        counts = count_slot_catalog(conn)
        print(f"Slot expected-unit catalog: {counts['total']} entries")
        print(f"  high: {counts['high']}")
        print(f"  medium: {counts['medium']}")
        print(f"  low: {counts['low']}")
        print()
        print("Pass one of --show-high-confidence, --show-scattered, "
              "--show-coverage for details.")
        conn.close()
        return 0

    import json as _json
    if args.json:
        print(_json.dumps({"view": view, "rows": rows}, indent=2))
    else:
        print(f"=== {view} ({len(rows)} rows) ===")
        for r in rows[: args.limit]:
            print(
                f"  {r['physics_type']}[{r['sdim']}] "
                f"{r['feature_type']}/{r['slot_property']:25s} "
                f"({r['feature_scope']})"
            )
            print(
                f"    modal={r.get('modal_unit')}  "
                f"n={r.get('n_attempts')}  "
                f"resolved={r.get('n_resolved')}  "
                f"coverage={r.get('coverage_frac'):.2f}  "
                f"modal_frac={(r.get('modal_frac') or 0):.2f}  "
                f"confidence={r.get('confidence')}"
            )
            dist = r.get("distribution") or {}
            if dist:
                top = sorted(
                    dist.items(), key=lambda kv: -kv[1]
                )[:4]
                preview = ", ".join(
                    f"{u}:{n}" for u, n in top
                )
                if len(dist) > 4:
                    preview += f", +{len(dist) - 4} more"
                print(f"    distribution: {preview}")
        if len(rows) > args.limit:
            print(f"  ... {len(rows) - args.limit} more rows suppressed")

    conn.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search the COMSOL API knowledge base (Javadoc + Reference Manual).

    Same backend as the MCP ``search_api`` tool: FTS5 over the ``knowledge``
    table, optionally restricted to one build stage. Until 2026-08 this verb
    searched the orchestrator's build catalog instead — a table nothing ever
    wrote to — so every query answered "no builds"; campaign agents logged
    that as "search has no API-knowledge backend".
    """
    config = Config.from_yaml(Path("comsol_support/config.yaml"))
    if args.db:
        config.db_path = Path(args.db)

    conn = init_db(config.db_path)
    try:
        try:
            rows = search_knowledge(
                conn, args.query, stage=args.stage, limit=args.limit,
            )
        except sqlite3.OperationalError as e:
            print(f"Search error: query '{args.query}' could not be "
                  f"processed ({e}). Try simpler keywords or quote phrases.")
            return 1

        if not rows:
            total = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            where = f" (stage={args.stage})" if args.stage else ""
            if total == 0:
                print(f"No API entries matching '{args.query}' — the knowledge "
                      f"base at {config.db_path} is empty. Populate it with "
                      f"`comsol-support scrape javadoc` and "
                      f"`comsol-support scrape refmanual` (or set COMSOL_DB to "
                      f"a populated database).")
            else:
                print(f"No API entries matching '{args.query}'{where} "
                      f"({total} entries indexed).")
            return 0

        print("\n\n".join(format_knowledge_row(r) for r in rows))
        return 0
    finally:
        conn.close()


def cmd_promote_catalog(args: argparse.Namespace) -> int:
    """Parse a corpus of already-converted .java files and rewrite
    native_interfaces.corpus_freq on matching catalog rows.

    Robustness: missing or empty java-dir returns exit code 2 with a
    clear stderr message. Any class_name appearing in corpus but
    absent from the catalog is reported but does not fail the command.
    """
    from comsol_support.corpus_miner import analyze_corpus
    from comsol_support.native_catalog import update_catalog_corpus_freq

    java_dir = Path(args.java_dir)
    if not java_dir.is_dir():
        print(
            f"promote-catalog: --java-dir does not exist or is not a "
            f"directory: {java_dir}",
            file=sys.stderr,
        )
        return 2

    # analyze_corpus walks the tree itself; an empty tree returns zero-
    # frequency dicts, in which case update is a no-op.
    stats = analyze_corpus(java_dir)
    if stats.total_models == 0:
        print(
            f"promote-catalog: no .java files found under {java_dir}",
            file=sys.stderr,
        )
        return 2

    conn = init_db(Path(args.db))
    try:
        summary = update_catalog_corpus_freq(conn, stats)
    finally:
        conn.close()

    print(
        f"promote-catalog: analyzed {stats.parsed} models "
        f"({stats.failed} failed parse), "
        f"updated {summary['matched']} catalog row(s)"
    )
    if summary["unmatched"]:
        print(
            f"  {len(summary['unmatched'])} class_name(s) seen in corpus "
            "but not in catalog (review seed to promote):"
        )
        # Cap at 20 lines to keep output tractable.
        for stage, cls, count in summary["unmatched"][:20]:
            print(f"    [{stage}] {cls}: freq={count}")
        if len(summary["unmatched"]) > 20:
            print(f"    … and {len(summary['unmatched']) - 20} more")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="comsol-support",
        description="COMSOL model-code generation, execution, and linting "
                    "tools for COMSOL Multiphysics 6.4 (other versions are "
                    "unverified — see COMSOL_VERSIONS.md)",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")

    subs = parser.add_subparsers(dest="command", help="Available commands")

    # search — API knowledge base (same backend as the MCP search_api tool)
    p_search = subs.add_parser(
        "search",
        help="Search the COMSOL API knowledge base (Javadoc + Reference Manual)")
    p_search.add_argument(
        "query",
        help="FTS5 query, e.g. 'Swept sourceface' or '\"boundary layer\"'")
    p_search.add_argument(
        "--stage",
        help="Restrict to one build stage (geometry, selections, materials, "
             "physics, mesh, studies, postprocessing, functions, parameters)")
    p_search.add_argument("--limit", "-n", type=int, default=10,
                          help="Max results (default: 10)")
    p_search.add_argument("--db", help="Override database path")

    # scrape (with subcommands: javadoc, refmanual)
    p_scrape = subs.add_parser("scrape", help="Scrape COMSOL docs into knowledge base")
    scrape_subs = p_scrape.add_subparsers(dest="scrape_target",
                                           help="What to scrape")

    # scrape javadoc
    p_scrape_jd = scrape_subs.add_parser("javadoc", help="Scrape Javadoc API docs")
    p_scrape_jd.add_argument("--api-dir", help="Path to Javadoc API directory")
    p_scrape_jd.add_argument("--version", default="6.4",
                             help="COMSOL version label (default: 6.4)")
    p_scrape_jd.add_argument("--db", help="Override database path")

    # scrape refmanual
    p_scrape_rm = scrape_subs.add_parser("refmanual",
                                          help="Scrape Reference Manual PDF")
    p_scrape_rm.add_argument("--pdf", help="Path to Reference Manual PDF")
    p_scrape_rm.add_argument("--version", default="6.4",
                             help="COMSOL version label (default: 6.4)")
    p_scrape_rm.add_argument("--db", help="Override database path")

    # scrape corpus
    p_scrape_corpus = scrape_subs.add_parser("corpus",
        help="Mine .mph application models into fragment table")
    p_scrape_corpus.add_argument("--applications-dir",
        help="Path to COMSOL applications directory")
    p_scrape_corpus.add_argument("--comsol-path",
        help="Path to COMSOL installation root")
    p_scrape_corpus.add_argument("--output-dir", default="corpus",
        help="Where to write converted .java files (default: corpus)")
    p_scrape_corpus.add_argument("--version", default="6.4",
        help="COMSOL version label (default: 6.4)")
    p_scrape_corpus.add_argument("--skip-conversion", action="store_true",
        help="Skip .mph->.java conversion, analyze existing .java files only")
    p_scrape_corpus.add_argument("--verify-only", action="store_true",
        help="Only run environment verification (Phase A)")
    p_scrape_corpus.add_argument("--db", help="Override database path")

    # scrape slots — Phase 1 of the slot expected-unit catalog pipeline
    p_scrape_slots = scrape_subs.add_parser("slots",
        help="Harvest physics-feature slots into a JSONL dump (Phase 1)")
    p_scrape_slots.add_argument("--output", "-o", required=True,
        help="Path to append JSONL dump records (created if missing)")
    p_scrape_slots.add_argument("--applications-dir",
        help="Path to COMSOL applications directory")
    p_scrape_slots.add_argument("--comsol-path",
        help="Path to COMSOL installation root")
    p_scrape_slots.add_argument("--workspace",
        help="Workspace for SlotHarvester .class "
             "(default: slot_harvest_workspace)")
    p_scrape_slots.add_argument("--version",
        help="COMSOL version label stamped into dump records "
             "(default: auto-detected from the install directory)")
    p_scrape_slots.add_argument("--max-models", type=int,
        help="Cap number of models harvested (for smoke tests)")
    p_scrape_slots.add_argument("--timeout", type=int, default=7200,
        help="Total subprocess timeout seconds (default: 7200)")
    p_scrape_slots.add_argument("--harvest-only", action="store_true",
        help="Run Phase 1 only; skip aggregation into SQLite")
    p_scrape_slots.add_argument("--aggregate-only", action="store_true",
        help="Skip Phase 1; aggregate existing dump into SQLite")
    p_scrape_slots.add_argument("--clear-catalog", action="store_true",
        help="Delete existing slot_expected_units rows before aggregating")
    p_scrape_slots.add_argument("--db", help="Override database path")

    # slot-stats — Phase 4 of the slot expected-unit catalog pipeline
    p_stats = subs.add_parser("slot-stats",
        help="Report on the slot expected-unit catalog")
    stats_view = p_stats.add_mutually_exclusive_group()
    stats_view.add_argument("--show-scattered", action="store_true",
        help="List keys with n_attempts>=5 and low modal_frac "
             "(subvariant-discovery candidates)")
    stats_view.add_argument("--show-high-confidence", action="store_true",
        help="List high-confidence catalog entries, sorted by n_attempts")
    stats_view.add_argument("--show-coverage", action="store_true",
        help="List keys with low coverage_frac (Source B enrichment candidates)")
    stats_view.add_argument("--suggest-thresholds", action="store_true",
        help="Analyze catalog distributions and propose tuned "
             "confidence-tier thresholds")
    p_stats.add_argument("--target-high", type=int, default=50,
        help="(with --suggest-thresholds) Desired approximate high-tier "
             "population (default: 50)")
    p_stats.add_argument("--target-medium", type=int, default=200,
        help="(with --suggest-thresholds) Desired approximate medium-tier "
             "population (default: 200)")
    p_stats.add_argument("--limit", type=int, default=50,
        help="Max rows to print in tabular mode (default: 50)")
    p_stats.add_argument("--json", action="store_true",
        help="Emit JSON instead of tabular output")
    p_stats.add_argument("--db", help="Override database path")

    # mphgen — generate a GUI-openable .mph from a Java builder
    add_mphgen_subparser(subs)

    # edit-mph — load existing .mph, optionally mutate + solve, save
    add_editmph_subparser(subs)

    # run-harness — compile + run an arbitrary COMSOL-dependent class
    add_run_harness_subparser(subs)

    # query-mph — read-only introspection of an existing .mph (never saves)
    add_query_mph_subparser(subs)

    # license-status — bounded checkout probe for seat availability
    add_license_status_subparser(subs)

    # check — static + runtime linting for units and descriptions
    add_check_subparser(subs)

    # doctor — cross-platform install preflight / diagnostics
    add_doctor_subparser(subs)

    # gotcha-search — query known-gotchas.md by keyword or regex
    add_gotcha_search_subparser(subs)

    # ingest-telemetry — backfill JSONL sidecars into the DB
    add_ingest_telemetry_subparser(subs)

    # promote-catalog — corpus-derived native_interfaces.corpus_freq update
    p_promote = subs.add_parser(
        "promote-catalog",
        help=(
            "Update native_interfaces.corpus_freq from corpus-derived "
            "class-name counts. Parses already-converted .java files "
            "(from the corpus mining pipeline) and rewrites corpus_freq "
            "on matching catalog rows. Idempotent. Non-matching classes "
            "are logged, never auto-inserted."
        ),
    )
    p_promote.add_argument(
        "--java-dir", required=True,
        help="Directory tree of .java files from the corpus converter "
             "(e.g., the output of `scrape corpus`).",
    )
    p_promote.add_argument(
        "--db", required=True, help="Path to the SQLite database.",
    )

    return parser


SUPPORTED_COMSOL = "6.4"


def _warn_if_not_comsol_64() -> None:
    """comsol-support targets COMSOL Multiphysics 6.4 only. Say so, once
    per invocation, whenever the discovered install is something else —
    users must know this before they trust any output. Never raises;
    `doctor` gives the full picture."""
    try:
        from comsol_support import COMSOL_PATH
        from comsol_support.doctor import comsol_version
        root = Path(COMSOL_PATH)
        if not root.is_dir():
            return
        version = comsol_version(root)
        if version == "unknown" or version.startswith(SUPPORTED_COMSOL):
            return
        print(f"warning: COMSOL {version} found at {root} — comsol-support "
              f"supports COMSOL Multiphysics {SUPPORTED_COMSOL} only; results "
              f"on other versions are unverified (COMSOL_VERSIONS.md). Set "
              f"COMSOL_PATH to a {SUPPORTED_COMSOL} install.", file=sys.stderr)
    except Exception:  # noqa: BLE001 — advisory only
        return


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(verbose=getattr(args, "verbose", False))

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "search": cmd_search,
        "scrape": cmd_scrape,
        "slot-stats": cmd_slot_stats,
        "mphgen": cmd_mphgen,
        "edit-mph": cmd_editmph,
        "run-harness": cmd_run_harness,
        "query-mph": cmd_query_mph,
        "license-status": cmd_license_status,
        "check": cmd_check,
        "doctor": cmd_doctor,
        "gotcha-search": cmd_gotcha_search,
        "ingest-telemetry": cmd_ingest_telemetry,
        "promote-catalog": cmd_promote_catalog,
    }

    handler = dispatch.get(args.command)
    if handler:
        if args.command != "doctor":
            _warn_if_not_comsol_64()
        from comsol_support._source_checkout import SourceCheckoutRequired
        try:
            rc = handler(args)
        except SourceCheckoutRequired as e:
            print(f"error: {e}", file=sys.stderr)
            rc = 2
        sys.exit(rc)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
