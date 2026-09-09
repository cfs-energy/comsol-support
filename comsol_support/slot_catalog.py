"""Phase 2 of the slot expected-unit catalog pipeline.

Consumes the JSONL dump produced by slot_harvest.run_harvest and writes
two tables into SQLite:

  - `slot_expected_units` — the prescriptive catalog keyed on the
    five-axis subvariant tuple. Each row carries the resolved-unit
    distribution, modal unit, coverage_frac, and confidence tier.
  - `variable_declared_units` — Source-B records that feed Layer C's
    symbol resolver indirectly.

Design decisions:
  - Only `resolved` deduce outcomes contribute to the distribution.
  - `coverage_frac = n_resolved / n_attempts` is a first-class confidence
    signal alongside `modal_frac`.
  - Confidence thresholds are module-level constants (no config
    framework).
  - Feature type with `type_source='interface_fallback'` is kept in a
    separate pool logically (same row, distinct column) so drift is
    auditable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from comsol_support.db import (
    upsert_slot_expected_unit,
    upsert_variable_declared_unit,
)
from comsol_support.dimensional import (
    AnalysisRequest,
    DimensionalError,
    DimensionalFinding,
    analyze,
    normalise_unit_string,
)
from comsol_support.slot_harvest import iter_dump

logger = logging.getLogger(__name__)


# ---- Threshold constants ----

N_HIGH = 20
MODAL_HIGH = 0.9
COVERAGE_HIGH = 0.5
N_MED = 5
MODAL_MED = 0.7

# Batch size for Layer C calls. Historical: each batch was one
# wolframscript subprocess; the engine is in-process pure Python since
# pure Python, so batching now only bounds memory/log granularity.
DEFAULT_BATCH_SIZE = 500

# Current slot-record schema version expected by the aggregator. Keep
# in sync with SlotHarvester.java's SCHEMA_VERSION. The aggregator
# skips records whose schema_version is missing or greater than this;
# smaller versions trigger a warning but are still ingested (forward-
# compatibility: older dumps remain readable by newer aggregators).
EXPECTED_SCHEMA_VERSION = 1

# Unit strings that are trivially dimensionless. A modal unit matching
# any of these cannot reach 'high' confidence — they don't meaningfully
# constrain Layer C's CompatibleUnitQ check and in practice arise either
# from genuine unitless slots (count, fraction, enum-as-identifier) or
# from Layer C's default-to-dimensionless behaviour on unknown
# identifiers. Either way, catalog entries keyed to these provide no
# downstream prescriptive value. Low/medium tiers still record them for
# audit and for the scatter report.
DIMENSIONLESS_UNIT_MARKERS = frozenset({
    "",                      # empty
    "1",                     # unit-one
    "DimensionlessUnit",     # Mathematica symbol
    '"DimensionlessUnit"',   # quoted serialisation
    "dimensionless",         # lowercase alias
    "None",                  # Python None serialised
})


def is_dimensionless_unit(unit: str | None) -> bool:
    """True when the unit string is a trivially-dimensionless marker."""
    if unit is None:
        return True
    stripped = unit.strip().strip('"').strip()
    return stripped in DIMENSIONLESS_UNIT_MARKERS or stripped == ""


# ---- Data ----

@dataclass
class AggregationReport:
    """Summary of a catalog aggregation run."""
    slots_read: int = 0
    slots_analyzed: int = 0
    slots_resolved: int = 0
    slots_unresolved: int = 0
    slots_translation_error: int = 0
    catalog_keys: int = 0
    catalog_high: int = 0
    catalog_medium: int = 0
    catalog_low: int = 0
    var_unit_records: int = 0
    var_unit_keys: int = 0
    errors: list[str] = field(default_factory=list)


# ---- Outcome classification ----

def _deduce_outcome(finding: DimensionalFinding) -> tuple[str, str]:
    """Classify one Layer C finding as (outcome, unit).

    outcome is one of: resolved, unresolved, non_scalar, translation_error.
    unit is the normalised deduced unit (empty when outcome != resolved).
    """
    if finding.translation_error:
        return ("translation_error", "")
    if not finding.resolved:
        return ("unresolved", "")
    if finding.inconsistent_arithmetic:
        # Mathematica couldn't propagate consistent dims — treat as
        # unresolved for catalog purposes.
        return ("unresolved", "")
    deduced = (finding.deduced_unit or "").strip()
    if not deduced:
        return ("unresolved", "")
    # Tensor / array markers: Mathematica returns `{a, b, c}` or similar
    # for non-scalar quantities. Exclude these from the distribution.
    if deduced.startswith("{") or "List" in deduced:
        return ("non_scalar", "")
    try:
        return ("resolved", normalise_unit_string(deduced))
    except Exception:
        return ("resolved", deduced)


# ---- Confidence tiering ----

def classify_confidence(
    n_attempts: int, n_resolved: int, modal_count: int,
    modal_unit: str | None = None,
) -> tuple[str, float, float]:
    """Return (confidence, coverage_frac, modal_frac).

    Tier rules (with the coverage_frac floor and the
    dimensionless-marker demotion):
      high   — n_attempts >= N_HIGH AND modal_frac >= MODAL_HIGH
                                AND coverage_frac >= COVERAGE_HIGH
                                AND modal_unit is not trivially dimensionless
      medium — n_attempts >= N_MED  AND modal_frac >= MODAL_MED
                                AND modal_unit is not trivially dimensionless
      low    — anything else

    The dimensionless demotion is deliberate: a dimensionless expected
    unit does not meaningfully constrain Layer C's downstream
    CompatibleUnitQ check, so promoting it to 'high' has no prescriptive
    value. The row is still persisted for audit.
    """
    coverage = (n_resolved / n_attempts) if n_attempts > 0 else 0.0
    modal = (modal_count / n_resolved) if n_resolved > 0 else 0.0

    if is_dimensionless_unit(modal_unit):
        return ("low", coverage, modal)

    if (n_attempts >= N_HIGH and modal >= MODAL_HIGH
            and coverage >= COVERAGE_HIGH):
        return ("high", coverage, modal)
    if n_attempts >= N_MED and modal >= MODAL_MED:
        return ("medium", coverage, modal)
    return ("low", coverage, modal)


# ---- Symbol-table builder (Source B → Layer C) ----

def build_global_symbol_table(
    conn: sqlite3.Connection,
) -> dict[str, str]:
    """Build a Layer C symbol table seeded from variable_declared_units.

    Only variables with a single majority-declared unit across all rows
    are included (avoids injecting ambiguous symbols). Ambiguous vars
    are left unresolved; Layer C handles missing symbols gracefully.
    """
    rows = conn.execute(
        "SELECT var_name, declared_unit, SUM(n_occurrences) AS total "
        "FROM variable_declared_units "
        "GROUP BY var_name, declared_unit"
    ).fetchall()
    per_var: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        per_var[r["var_name"]][r["declared_unit"]] = int(r["total"])
    table: dict[str, str] = {}
    for var, units in per_var.items():
        if not units:
            continue
        total = sum(units.values())
        best_unit, best_count = max(units.items(), key=lambda kv: kv[1])
        if total > 0 and best_count / total >= 0.9:
            try:
                table[var] = normalise_unit_string(best_unit)
            except Exception:
                table[var] = best_unit
    return table


# ---- Aggregation ----

def aggregate_dump(
    dump_path: Path,
    conn: sqlite3.Connection,
    *,
    wolframscript_path: str | None = None,   # back-compat, ignored
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_s: int = 180,
    clear_existing: bool = False,
) -> AggregationReport:
    """Read the JSONL dump, run Layer C on expressions, write catalog.

    When `clear_existing` is True, existing rows in both catalog tables
    are deleted before insertion. Default is append-then-upsert: existing
    rows with the same PK are replaced, others preserved. Useful when
    mixing harvests from multiple COMSOL versions.
    """
    dump_path = Path(dump_path)
    report = AggregationReport()

    if clear_existing:
        conn.execute("DELETE FROM slot_expected_units")
        conn.execute("DELETE FROM variable_declared_units")

    # Pass 1 — ingest variable_declared_units (Source B) and count
    # slot records (to know the shape of the Layer C workload).
    var_unit_counter: Counter = Counter()
    slot_records: list[dict] = []
    skipped_schema = 0
    warned_older = False
    for rec in iter_dump(dump_path):
        sv = rec.get("schema_version")
        if sv is None or sv > EXPECTED_SCHEMA_VERSION:
            # Unknown-future schema or pre-schema legacy dump — skip.
            skipped_schema += 1
            continue
        if sv < EXPECTED_SCHEMA_VERSION and not warned_older:
            logger.warning(
                "Dump contains older schema (%s); expected %s. "
                "Reading with forward-compatible semantics; consider "
                "re-harvesting for best results.",
                sv, EXPECTED_SCHEMA_VERSION,
            )
            warned_older = True

        kind = rec.get("kind")
        if kind == "slot":
            slot_records.append(rec)
            report.slots_read += 1
        elif kind == "var_unit":
            key = (
                rec.get("var_name", ""),
                rec.get("physics_type", "unknown"),
                rec.get("sdim", "unknown"),
                rec.get("declaration_key", ""),
                rec.get("declared_unit", ""),
                rec.get("comsol_version", "unknown"),
            )
            var_unit_counter[key] += 1
            report.var_unit_records += 1

    if skipped_schema:
        msg = (
            f"{skipped_schema} records skipped due to missing or "
            f"unknown-future schema_version (expected {EXPECTED_SCHEMA_VERSION})"
        )
        logger.warning(msg)
        report.errors.append(msg)

    for key, n in var_unit_counter.items():
        (var_name, physics_type, sdim, decl_key, decl_unit, cv) = key
        upsert_variable_declared_unit(
            conn, var_name=var_name, physics_type=physics_type,
            sdim=sdim, declaration_key=decl_key, declared_unit=decl_unit,
            n_occurrences=n, comsol_version=cv,
        )
    report.var_unit_keys = len(var_unit_counter)
    conn.commit()

    # Seed Layer C's global symbol table from variable declarations.
    symbol_table = build_global_symbol_table(conn)
    logger.info(
        "Source B seeded symbol table with %d variables", len(symbol_table)
    )

    # Pass 2 — run Layer C on every slot expression in batches.
    #
    # Each slot record becomes one AnalysisRequest. The id encodes the
    # position in slot_records so we can pair findings back to records.
    requests: list[AnalysisRequest] = []
    for i, rec in enumerate(slot_records):
        expr = rec.get("expression") or ""
        if not expr.strip():
            continue
        requests.append(AnalysisRequest(
            id=f"r{i}",
            expression=expr,
            expected_unit="",
            symbol_table=None,  # use global table passed to analyze()
        ))

    findings_by_id: dict[str, DimensionalFinding] = {}
    if requests:
        for start in range(0, len(requests), batch_size):
            chunk = requests[start:start + batch_size]
            try:
                findings = analyze(
                    chunk, symbol_table,
                    wolframscript_path=wolframscript_path,
                    timeout_s=timeout_s,
                )
            except DimensionalError as e:
                logger.warning(
                    "Layer C batch starting at %d failed: %s", start, e
                )
                report.errors.append(
                    f"batch_{start}: {e}"
                )
                continue
            for f in findings:
                findings_by_id[f.id] = f
            logger.info(
                "aggregate: batch %d/%d analyzed",
                start // batch_size + 1,
                (len(requests) + batch_size - 1) // batch_size,
            )

    # Pass 3 — bucket resolved outcomes by the five-axis key.
    buckets: dict[tuple, dict] = {}
    for i, rec in enumerate(slot_records):
        key = (
            rec.get("physics_type", "unknown"),
            rec.get("sdim", "unknown"),
            rec.get("feature_type", "unknown"),
            rec.get("feature_scope", "unknown"),
            rec.get("slot_property", ""),
            rec.get("comsol_version", "unknown"),
        )
        bucket = buckets.setdefault(key, {
            "n_attempts": 0,
            "n_resolved": 0,
            "distribution": Counter(),
            # Keep type_source consistent — if any record used a fallback,
            # mark the whole key as using a fallback. Over-cautious by
            # design.
            "type_source": "getType",
        })
        bucket["n_attempts"] += 1
        if rec.get("feature_type_source") == "interface_fallback":
            bucket["type_source"] = "interface_fallback"

        # Match the finding (may be absent if expression was empty).
        f = findings_by_id.get(f"r{i}")
        if f is None:
            report.slots_unresolved += 1
            continue

        outcome, unit = _deduce_outcome(f)
        if outcome == "translation_error":
            report.slots_translation_error += 1
        elif outcome == "resolved":
            bucket["n_resolved"] += 1
            bucket["distribution"][unit] += 1
            report.slots_resolved += 1
        else:
            report.slots_unresolved += 1
        report.slots_analyzed += 1

    # Pass 4 — write catalog rows.
    for key, bucket in buckets.items():
        (physics_type, sdim, feature_type, feature_scope, slot_property,
         comsol_version) = key
        dist: Counter = bucket["distribution"]
        n_attempts = bucket["n_attempts"]
        n_resolved = bucket["n_resolved"]

        if dist:
            modal_unit, modal_count = dist.most_common(1)[0]
        else:
            modal_unit, modal_count = (None, 0)

        confidence, coverage_frac, modal_frac = classify_confidence(
            n_attempts, n_resolved, modal_count,
            modal_unit=modal_unit,
        )
        upsert_slot_expected_unit(
            conn,
            physics_type=physics_type, sdim=sdim,
            feature_type=feature_type, feature_scope=feature_scope,
            slot_property=slot_property, comsol_version=comsol_version,
            n_attempts=n_attempts, n_resolved=n_resolved,
            coverage_frac=coverage_frac,
            modal_unit=modal_unit,
            modal_count=(modal_count if dist else None),
            modal_frac=(modal_frac if dist else None),
            distribution=dict(dist),
            confidence=confidence,
            type_source=bucket["type_source"],
        )

        report.catalog_keys += 1
        if confidence == "high":
            report.catalog_high += 1
        elif confidence == "medium":
            report.catalog_medium += 1
        else:
            report.catalog_low += 1

    conn.commit()
    return report


# ---- Scattered-key report (hook for Phase 4) ----

def scattered_keys(
    conn: sqlite3.Connection,
    *,
    n_min: int = N_MED,
    modal_max: float = MODAL_MED,
) -> list[dict]:
    """Return keys with enough samples to be trusted but low modal purity.

    These are the subvariant-discovery candidates: scatter across the
    same five-axis tuple suggests a missing sixth axis.
    """
    rows = conn.execute(
        """
        SELECT * FROM slot_expected_units
        WHERE n_attempts >= ?
              AND (modal_frac IS NULL OR modal_frac < ?)
        ORDER BY n_attempts DESC
        """,
        (n_min, modal_max),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["distribution"] = json.loads(d.get("distribution") or "{}")
        out.append(d)
    return out


def high_confidence_entries(conn: sqlite3.Connection) -> list[dict]:
    """Return all high-confidence catalog entries, sorted by n_attempts."""
    rows = conn.execute(
        """
        SELECT * FROM slot_expected_units
        WHERE confidence = 'high'
        ORDER BY n_attempts DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["distribution"] = json.loads(d.get("distribution") or "{}")
        out.append(d)
    return out


def harvest_single_model(
    mph_path: Path,
    *,
    comsol_path: str | Path,
    workspace_dir: Path,
    timeout_s: int = 600,
) -> list[dict]:
    """Run SlotHarvester on one .mph and return the parsed slot records.

    Uses the existing Phase 1 run_harvest under the hood, with a fresh
    temp dump file. Returns only slot records (var_unit records are
    filtered out). If harvest fails, returns an empty list — caller
    falls back to no catalog-derived overrides.
    """
    import tempfile
    from comsol_support.slot_harvest import run_harvest, read_dump

    with tempfile.TemporaryDirectory(prefix="slot_single_") as td:
        dump = Path(td) / "dump.jsonl"
        run_harvest(
            [Path(mph_path)], dump,
            comsol_path=comsol_path,
            workspace_dir=workspace_dir,
            timeout_s=timeout_s,
            progress=False,
        )
        if not dump.exists():
            return []
        records = read_dump(dump)
    return [r for r in records if r.get("kind") == "slot"]


def build_expected_overrides_from_slots(
    slot_records: list[dict],
    conn: sqlite3.Connection,
    *,
    comsol_version: str,
    min_confidence: str = "high",
) -> tuple[dict[str, str], list[dict]]:
    """Build an ``expected_overrides`` dict for analyze_model from slot records.

    ``slot_records`` is the Phase 1 harvester's per-slot output for a single
    model (each dict has the five-axis key fields and an expression).

    For each slot whose key resolves to a catalog row at or above
    ``min_confidence``, we add an entry to ``expected_overrides`` keyed as
    ``slot:<physics_tag>/<feature_tag>:<slot_property>`` with the modal
    unit as the expected value. ``analyze_model`` treats this just like
    a variable override.

    Returns ``(expected_overrides, annotations)`` where ``annotations``
    is a list of per-slot dicts describing what the catalog returned —
    useful for downstream sidecar emission and auditing (e.g. a
    version_mismatch annotation).
    """
    from comsol_support.db import lookup_expected_unit

    overrides: dict[str, str] = {}
    annotations: list[dict] = []
    for rec in slot_records:
        physics_tag = rec.get("physics_tag", "?")
        feature_tag = rec.get("feature_tag", "?")
        slot_property = rec.get("slot_property", "")
        if not slot_property:
            continue

        row = lookup_expected_unit(
            conn,
            physics_type=rec.get("physics_type", "unknown"),
            sdim=rec.get("sdim", "unknown"),
            feature_type=rec.get("feature_type", "unknown"),
            feature_scope=rec.get("feature_scope", "unknown"),
            slot_property=slot_property,
            comsol_version=comsol_version,
            min_confidence=min_confidence,
        )
        if row is None:
            continue

        key = (
            f"slot:{physics_tag}/{feature_tag}:{slot_property}"
        )
        unit = row.get("modal_unit") or ""
        if not unit or is_dimensionless_unit(unit):
            continue
        overrides[key] = unit
        annotations.append({
            "key": key,
            "expected_unit": unit,
            "confidence": row.get("confidence"),
            "n_attempts": row.get("n_attempts"),
            "coverage_frac": row.get("coverage_frac"),
            "modal_frac": row.get("modal_frac"),
            "version_mismatch": row.get("version_mismatch", False),
        })
    return overrides, annotations


def suggest_thresholds(
    conn: sqlite3.Connection,
    *,
    target_high_count: int = 50,
    target_medium_count: int = 200,
) -> dict:
    """Propose tuned confidence thresholds based on the current catalog.

    Reads every row's (n_attempts, modal_frac, coverage_frac, modal_unit)
    and computes percentile-based threshold candidates such that the
    resulting tier populations approach the given targets. Output is
    advisory only — the caller applies the values by editing
    `slot_catalog.py` module constants.

    Shape of the return value:
      {
        "current": {"N_HIGH", "MODAL_HIGH", "COVERAGE_HIGH",
                    "N_MED", "MODAL_MED"},
        "observed": {"rows", "high", "medium", "low", ...},
        "suggested": {"N_HIGH", "MODAL_HIGH", "COVERAGE_HIGH",
                      "N_MED", "MODAL_MED"},
        "rationale": [..short strings..]
      }

    Robustness: no suggestion is made when the catalog is empty or has
    too few non-trivially-dimensional rows to be informative. In those
    cases the helper returns `current` values with a rationale note.
    """
    rows = conn.execute(
        "SELECT n_attempts, modal_frac, coverage_frac, modal_unit, confidence "
        "FROM slot_expected_units"
    ).fetchall()

    current = {
        "N_HIGH": N_HIGH, "MODAL_HIGH": MODAL_HIGH,
        "COVERAGE_HIGH": COVERAGE_HIGH,
        "N_MED": N_MED, "MODAL_MED": MODAL_MED,
    }
    observed = {
        "rows": len(rows),
        "high": sum(1 for r in rows if r["confidence"] == "high"),
        "medium": sum(1 for r in rows if r["confidence"] == "medium"),
        "low": sum(1 for r in rows if r["confidence"] == "low"),
    }
    rationale: list[str] = []

    # Filter to dimensionally-meaningful rows (the ones that can reach
    # high/medium tiers). Dimensionless entries can't be reclassified
    # up regardless of threshold choice.
    candidates = [
        r for r in rows
        if r["modal_unit"] and not is_dimensionless_unit(r["modal_unit"])
        and r["n_attempts"] is not None
        and r["modal_frac"] is not None
        and r["coverage_frac"] is not None
    ]
    observed["promotable_rows"] = len(candidates)

    if len(candidates) < 20:
        rationale.append(
            f"Too few promotable rows ({len(candidates)}) to tune "
            f"thresholds reliably — run a larger harvest first "
            f"(recommend ≥100 models)."
        )
        return {
            "current": current,
            "observed": observed,
            "suggested": dict(current),
            "rationale": rationale,
        }

    n_attempts = sorted(int(r["n_attempts"]) for r in candidates)
    modal_fracs = sorted(float(r["modal_frac"]) for r in candidates)
    coverage_fracs = sorted(float(r["coverage_frac"]) for r in candidates)

    def pct(xs: list, p: float):
        if not xs:
            return None
        i = max(0, min(len(xs) - 1, int(round(p * (len(xs) - 1)))))
        return xs[i]

    # Choose N_HIGH so that roughly the top `target_high_count`
    # promotable rows would pass the n_attempts threshold. Clamp to at
    # least the previous N_HIGH so we never weaken the bar.
    if target_high_count < len(candidates):
        p_high = 1.0 - (target_high_count / len(candidates))
    else:
        p_high = 0.0
    sug_n_high = max(N_HIGH, int(pct(n_attempts, p_high) or N_HIGH))

    if target_medium_count < len(candidates):
        p_med = 1.0 - (target_medium_count / len(candidates))
    else:
        p_med = 0.0
    sug_n_med = max(
        N_MED,
        min(sug_n_high - 1, int(pct(n_attempts, p_med) or N_MED)),
    )

    # For modal purity, keep high bar strict (never relax below 0.85)
    # and suggest pulling MODAL_MED up if the 75th-percentile of modal
    # purity is high (meaning most rows are unimodal).
    p75_modal = pct(modal_fracs, 0.25) or MODAL_MED  # 25th-percentile ASC = 75th by rank
    sug_modal_high = max(MODAL_HIGH, 0.85)
    sug_modal_med = max(MODAL_MED, min(0.9, round(p75_modal, 2)))

    # Coverage floor — suggest median of observed coverages, clamped.
    med_cov = pct(coverage_fracs, 0.5) or COVERAGE_HIGH
    sug_coverage_high = max(0.3, min(0.7, round(med_cov, 2)))

    suggested = {
        "N_HIGH": int(sug_n_high),
        "MODAL_HIGH": float(sug_modal_high),
        "COVERAGE_HIGH": float(sug_coverage_high),
        "N_MED": int(sug_n_med),
        "MODAL_MED": float(sug_modal_med),
    }

    changed = {k: (current[k], suggested[k]) for k in current
               if current[k] != suggested[k]}
    if changed:
        rationale.append(
            f"Target tier populations: ≤{target_high_count} high, "
            f"≤{target_medium_count} medium (out of "
            f"{len(candidates)} promotable rows)."
        )
        for k, (old, new) in changed.items():
            rationale.append(f"  {k}: {old} → {new}")
    else:
        rationale.append(
            "Current thresholds align with observed distribution; "
            "no tuning suggested."
        )

    return {
        "current": current,
        "observed": observed,
        "suggested": suggested,
        "rationale": rationale,
    }


def low_coverage_keys(
    conn: sqlite3.Connection,
    *,
    coverage_max: float = COVERAGE_HIGH,
    n_min: int = N_MED,
) -> list[dict]:
    """Return keys with enough samples but low coverage_frac.

    These are candidates for Source B enrichment — a lot of expressions
    but few are resolvable by Layer C. More symbol table seeding could
    help.
    """
    rows = conn.execute(
        """
        SELECT * FROM slot_expected_units
        WHERE n_attempts >= ? AND coverage_frac < ?
        ORDER BY coverage_frac ASC
        """,
        (n_min, coverage_max),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["distribution"] = json.loads(d.get("distribution") or "{}")
        out.append(d)
    return out
