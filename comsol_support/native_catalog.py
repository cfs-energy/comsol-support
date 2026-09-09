"""Native-interface catalog — the data layer for the native-preference plan.

Backs three MCP tools (list_physics_options, list_studies_for_physics,
list_default_plots_for_physics) — the retrieval side of the native-first
ladders in docs/modeling-practice.md.

Design properties:
- Flat on disk (native_interfaces + domain_synonyms tables live in db.py).
- Rendered nested by callers that need a domain-grouped view.
- Idempotent seed: re-runs refresh curated metadata (class_name, notes)
  without clobbering corpus_freq and setup_cost_rank values maintained
  by the promote-catalog job.
- Hand-curated starter content covering the most common COMSOL domains.
- Extended by the offline corpus-promotion job over time via
  update_catalog_corpus_freq().

No external dependencies — stdlib sqlite3 + json only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterable

logger = logging.getLogger("comsol_support.native_catalog")

# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def upsert_interface(
    conn: sqlite3.Connection,
    *,
    domain_keyword: str,
    stage: str,
    tag_prefix: str,
    class_name: str,
    setup_cost_rank: int,
    corpus_freq: int = 0,
    default_studies: list[str] | None = None,
    default_plots: list[str] | None = None,
    auto_features: list[str] | None = None,
    notes: str = "",
) -> None:
    """Insert-or-refresh one native_interfaces row.

    On first insert: all columns written as given.
    On conflict (PK = domain_keyword+stage+tag_prefix): curated metadata
    (class_name, default_studies, default_plots, auto_features, notes) is
    REFRESHED to the current seed value — so updates to the seed file
    propagate to existing DBs. But setup_cost_rank and corpus_freq are
    PRESERVED — those carry values maintained by the promote-catalog job
    and must not be clobbered by a re-seed.
    """
    conn.execute(
        """
        INSERT INTO native_interfaces
          (domain_keyword, stage, tag_prefix, class_name,
           setup_cost_rank, corpus_freq,
           default_studies, default_plots, auto_features, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain_keyword, stage, tag_prefix) DO UPDATE SET
          class_name      = excluded.class_name,
          default_studies = excluded.default_studies,
          default_plots   = excluded.default_plots,
          auto_features   = excluded.auto_features,
          notes           = excluded.notes
        """,
        (
            domain_keyword, stage, tag_prefix, class_name,
            int(setup_cost_rank), int(corpus_freq),
            json.dumps(default_studies or []),
            json.dumps(default_plots or []),
            json.dumps(auto_features or []),
            notes,
        ),
    )


def upsert_synonym(
    conn: sqlite3.Connection,
    *,
    intent_keyword: str,
    domain_keyword: str,
) -> None:
    """Insert-or-ignore one domain_synonyms row."""
    conn.execute(
        "INSERT OR IGNORE INTO domain_synonyms "
        "(intent_keyword, domain_keyword) VALUES (?, ?)",
        (intent_keyword.lower().strip(), domain_keyword.lower().strip()),
    )


def update_catalog_corpus_freq(
    conn: sqlite3.Connection,
    stats,
) -> dict:
    """Replace `corpus_freq` on catalog rows with observed counts from a
    CorpusStatistics object.

    Joins corpus class-name counts to catalog rows by `class_name`:
      physics_freq[cls]  → UPDATE stage='physics'  WHERE class_name = cls
      study_freq[cls]    → UPDATE stage='study'    WHERE class_name = cls
      result_freq[cls]   → UPDATE stage='result'   WHERE class_name = cls

    Rows not mentioned in stats are left untouched (keep prior freq).
    Class names observed in stats but absent from the catalog are LOGGED
    at INFO level (observability — a human can promote them by editing
    the seed), but NOT auto-inserted. Per-row UPDATE is wrapped in
    try/except so a single bad row cannot abort the whole pass.

    Idempotent: running against the same stats twice yields the same
    final corpus_freq values (this is an absolute SET, not an increment).

    Args:
        conn: open sqlite3 connection to an init_db'd database.
        stats: corpus_miner.CorpusStatistics (duck-typed — only
            physics_freq, study_freq, result_freq are read).

    Returns:
        Summary dict with keys:
          matched: int — catalog rows updated
          unmatched: list[tuple[str, str, int]] — (stage, class_name, count)
                      for classes seen in corpus but absent from catalog
    """
    summary = {"matched": 0, "unmatched": []}
    freq_by_stage = {
        "physics": getattr(stats, "physics_freq", {}) or {},
        "study":   getattr(stats, "study_freq", {}) or {},
        "result":  getattr(stats, "result_freq", {}) or {},
    }
    for stage, freq in freq_by_stage.items():
        for class_name, count in freq.items():
            if not class_name:
                continue
            try:
                cursor = conn.execute(
                    """
                    UPDATE native_interfaces SET corpus_freq = ?
                    WHERE stage = ? AND class_name = ?
                    """,
                    (int(count), stage, class_name),
                )
            except sqlite3.Error as e:
                logger.warning(
                    "catalog update skipped for %s/%s: %s",
                    stage, class_name, e,
                )
                continue
            if cursor.rowcount > 0:
                summary["matched"] += cursor.rowcount
            else:
                summary["unmatched"].append((stage, class_name, int(count)))
                logger.info(
                    "corpus class %r (stage=%s, freq=%d) not in catalog; "
                    "review seed if this should be promoted",
                    class_name, stage, count,
                )
    conn.commit()
    return summary


def bump_corpus_freq(
    conn: sqlite3.Connection,
    *,
    domain_keyword: str,
    stage: str,
    tag_prefix: str,
    delta: int = 1,
) -> None:
    """Increment corpus_freq for one row. Used by the offline promotion
    job to push rare-but-successful custom choices up the ranking over
    time without hand-curation. Silently no-ops on missing rows."""
    conn.execute(
        """
        UPDATE native_interfaces SET corpus_freq = corpus_freq + ?
        WHERE domain_keyword = ? AND stage = ? AND tag_prefix = ?
        """,
        (int(delta), domain_keyword, stage, tag_prefix),
    )


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def lookup_domain(
    conn: sqlite3.Connection, intent_keyword: str,
) -> str | None:
    """Resolve an intent keyword to its canonical domain. Returns None
    when no synonym matches — callers inject no preamble block in that
    case (novel / unclassified intent)."""
    if not intent_keyword:
        return None
    row = conn.execute(
        "SELECT domain_keyword FROM domain_synonyms WHERE intent_keyword = ?",
        (intent_keyword.lower().strip(),),
    ).fetchone()
    return row["domain_keyword"] if row else None


def lookup_domains(
    conn: sqlite3.Connection, intent_keywords: Iterable[str],
) -> list[str]:
    """Resolve a list of intent keywords, de-duped, order-preserving."""
    seen: set[str] = set()
    result: list[str] = []
    for kw in intent_keywords:
        dom = lookup_domain(conn, kw)
        if dom and dom not in seen:
            seen.add(dom)
            result.append(dom)
    return result


def list_interfaces(
    conn: sqlite3.Connection,
    *,
    domain_keyword: str,
    stage: str = "physics",
    limit: int = 5,
) -> list[dict]:
    """Return native interfaces for (domain, stage), ranked by setup
    cost (lowest first), ties broken by highest corpus_freq.

    Each returned dict mirrors the row; list-valued columns are decoded
    back to Python lists. Empty list when nothing matches.
    """
    rows = conn.execute(
        """
        SELECT * FROM native_interfaces
        WHERE domain_keyword = ? AND stage = ?
        ORDER BY setup_cost_rank ASC, corpus_freq DESC, tag_prefix ASC
        LIMIT ?
        """,
        (domain_keyword, stage, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_interface(
    conn: sqlite3.Connection,
    *,
    tag_prefix: str,
    stage: str = "physics",
) -> dict | None:
    """Fetch one row by (tag_prefix, stage). tag_prefix is scoped by stage
    — the same tag (e.g. "pde") may appear in multiple domains at the
    physics stage with different ranks. Returns the lowest-rank row if
    so. None if not found."""
    row = conn.execute(
        """
        SELECT * FROM native_interfaces
        WHERE tag_prefix = ? AND stage = ?
        ORDER BY setup_cost_rank ASC, corpus_freq DESC
        LIMIT 1
        """,
        (tag_prefix, stage),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_studies_for_physics(
    conn: sqlite3.Connection, *, physics_tag: str, limit: int = 5,
) -> list[dict]:
    """Return study options referenced by a physics interface's
    default_studies, joined to the studies catalog for rendering info."""
    phys = get_interface(conn, tag_prefix=physics_tag, stage="physics")
    study_tags: list[str]
    if phys:
        study_tags = phys.get("default_studies") or []
    else:
        study_tags = []
    if not study_tags:
        # No physics-specific defaults — fall back to corpus-ranked study
        # catalog. Callers get "something useful" rather than nothing.
        rows = conn.execute(
            """
            SELECT * FROM native_interfaces WHERE stage = 'study'
            ORDER BY setup_cost_rank ASC, corpus_freq DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    placeholders = ",".join(["?"] * len(study_tags))
    rows = conn.execute(
        f"""
        SELECT * FROM native_interfaces
        WHERE stage = 'study' AND tag_prefix IN ({placeholders})
        ORDER BY setup_cost_rank ASC, corpus_freq DESC
        LIMIT ?
        """,
        (*study_tags, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_plots_for_physics(
    conn: sqlite3.Connection, *, physics_tag: str, limit: int = 5,
) -> list[dict]:
    """Return plot/probe options referenced by a physics interface's
    default_plots, joined to the results catalog."""
    phys = get_interface(conn, tag_prefix=physics_tag, stage="physics")
    plot_tags: list[str]
    if phys:
        plot_tags = phys.get("default_plots") or []
    else:
        plot_tags = []
    if not plot_tags:
        rows = conn.execute(
            """
            SELECT * FROM native_interfaces WHERE stage = 'result'
            ORDER BY setup_cost_rank ASC, corpus_freq DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    placeholders = ",".join(["?"] * len(plot_tags))
    rows = conn.execute(
        f"""
        SELECT * FROM native_interfaces
        WHERE stage = 'result' AND tag_prefix IN ({placeholders})
        ORDER BY setup_cost_rank ASC, corpus_freq DESC
        LIMIT ?
        """,
        (*plot_tags, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row into a plain dict with JSON fields decoded."""
    d = dict(row)
    for k in ("default_studies", "default_plots", "auto_features"):
        raw = d.get(k)
        if isinstance(raw, str) and raw:
            try:
                d[k] = json.loads(raw)
            except json.JSONDecodeError:
                d[k] = []
        elif raw is None:
            d[k] = []
    return d


# ---------------------------------------------------------------------------
# Hand-curated starter seed
# ---------------------------------------------------------------------------

# Curated covering the most common COMSOL domains. Kept intentionally small
# so the first build is grounded; the corpus-promotion job extends it.
#
# Rank semantics (setup_cost_rank): 1 = lowest-cost native option, 10 =
# generic PDE fallback. Ranks are coarse and relative within a domain.
# corpus_freq values are placeholder-curated — real numbers come from the
# 875-model corpus mining run.

_PHYSICS_SEED = [
    # Heat transfer
    ("heat", "ht",    "HeatTransfer",
     1, 200, ["stat", "time"], ["surf", "line", "probe_dom"], ["solid1", "init1"],
     "Heat Transfer in Solids — solid conduction, ambient losses. Default."),
    ("heat", "htfl",  "HeatTransferFluids",
     2, 80, ["stat", "time"], ["surf", "streamline", "probe_dom"], ["fluid1", "init1"],
     "Heat Transfer in Fluids — convective problems without full flow field."),
    ("heat", "nitf",  "NonIsothermalFlow",
     3, 40, ["stat", "time"], ["surf", "streamline", "arrow"], [],
     "Non-Isothermal Flow — coupled HT + Laminar Flow multiphysics."),
    ("heat", "pde",   "GeneralFormPDE",
     9, 3,  ["stat", "time"], ["surf", "line"], [],
     "Generic PDE — last resort if the native interfaces above do not fit."),

    # Fluid flow
    ("flow", "spf",   "LaminarFlow",
     1, 180, ["stat", "time"], ["surf", "streamline", "arrow"], ["fp1", "init1", "wall1"],
     "Laminar Flow — Navier-Stokes, Reynolds < ~2000 for pipes. Default."),
    ("flow", "keps",  "TurbulentFlowkEpsilon",
     2, 60, ["stat", "time"], ["surf", "streamline", "arrow"], [],
     "Turbulent Flow, k-epsilon — high-Reynolds flows with wall functions."),
    ("flow", "pde",   "GeneralFormPDE",
     9, 2,  ["stat", "time"], ["surf"], [],
     "Generic PDE — last resort if neither native flow interface fits."),

    # Structural mechanics
    ("stress", "solid", "SolidMechanics",
     1, 220, ["stat", "time", "eig"], ["surf", "line", "arrow"], ["lemm1", "free1", "init1"],
     "Solid Mechanics — linear and nonlinear elasticity. Default."),
    ("stress", "shell", "Shell",
     2, 40, ["stat", "eig"], ["surf"], [],
     "Shell — thin-walled structures (plates, membranes)."),
    ("stress", "beam",  "Beam",
     3, 25, ["stat", "eig"], ["line"], [],
     "Beam — slender structural members."),
    ("stress", "pde",   "GeneralFormPDE",
     9, 3,  ["stat"], ["surf"], [],
     "Generic PDE — last resort when native structural interfaces do not fit."),

    # Low-frequency electromagnetics
    ("em_low", "mf",   "MagneticFields",
     1, 110, ["stat", "time", "freq"], ["surf", "arrow"], ["al1", "init1"],
     "Magnetic Fields — AC/DC magnetics via vector potential A."),
    ("em_low", "ec",   "ElectricCurrents",
     1, 100, ["stat", "time", "freq"], ["surf", "arrow"], ["cucn1", "init1"],
     "Electric Currents — steady / quasistatic current flow in conductors."),
    ("em_low", "mef",  "MagneticFieldsNoCurrents",
     2, 40, ["stat"], ["surf", "arrow"], [],
     "Magnetic Fields, No Currents — magnetostatics via scalar potential."),

    # High-frequency / wave electromagnetics
    ("em_high", "emw",  "ElectromagneticWavesFrequencyDomain",
     1, 130, ["freq", "eig"], ["surf", "arrow"], ["wee1", "init1"],
     "Electromagnetic Waves, Frequency Domain — RF / microwave / photonic."),
    ("em_high", "ewfd", "ElectromagneticWavesTransient",
     2, 30, ["time"], ["surf", "arrow"], [],
     "Electromagnetic Waves, Transient — time-domain wave propagation."),

    # Acoustics
    ("acoustics", "acpr", "PressureAcoustics",
     1, 90, ["freq", "eig"], ["surf", "line"], ["fpam1", "init1"],
     "Pressure Acoustics — frequency-domain sound propagation. Default."),
    ("acoustics", "actd", "TransientPressureAcoustics",
     2, 20, ["time"], ["surf", "line"], [],
     "Transient Pressure Acoustics — time-domain sound propagation."),

    # Mass / species transport
    ("transport", "tds", "TransportOfDilutedSpecies",
     1, 70, ["stat", "time"], ["surf", "line"], ["cdm1", "init1"],
     "Transport of Diluted Species — convection-diffusion of solutes."),

    # Reaction kinetics
    ("chem", "chem", "ChemicalReactionEngineering",
     1, 50, ["time"], ["global", "line"], [],
     "Chemical Reaction Engineering — batch, CSTR, PFR reactors."),

    # Custom-PDE domain (explicit, for intents that name a PDE)
    ("custom", "pde",  "GeneralFormPDE",
     8, 5, ["stat", "time"], ["surf"], [],
     "General Form PDE — user-supplied flux and source terms."),
    ("custom", "cpde", "CoefficientFormPDE",
     9, 4, ["stat", "time"], ["surf"], [],
     "Coefficient Form PDE — linear PDE expressed via coefficients."),
    ("custom", "wpde", "WeakFormPDE",
     10, 2, ["stat", "time"], ["surf"], [],
     "Weak Form PDE — user-supplied weak-form integrand."),
]

_STUDIES_SEED = [
    # stage="study" rows. Same column shape; default_studies/plots unused.
    ("general", "stat",  "Stationary",
     1, 400, [], [], [], "Stationary — steady-state solve. Default for static problems."),
    ("general", "time",  "TimeDependent",
     2, 350, [], [], [], "Time Dependent — transient solve over a time interval."),
    ("general", "freq",  "FrequencyDomain",
     3, 150, [], [], [], "Frequency Domain — harmonic solve at one or more frequencies."),
    ("general", "eig",   "Eigenfrequency",
     3, 80,  [], [], [], "Eigenfrequency — modal analysis; returns eigenpairs."),
    ("general", "param", "ParametricSweep",
     4, 120, [], [], [], "Parametric Sweep — wraps a base step to sweep parameter values."),
]

_PLOTS_SEED = [
    # stage="result" rows. class_name must match what COMSOL's Java API
    # emits from .result(pg).feature().create(tag, ClassName) and from
    # .probe().create(tag, ClassName), so the corpus-frequency update
    # job can join on class_name directly. Names verified against
    # COMSOL 6.4 model examples.
    ("general", "surf",       "Surface",
     1, 400, [], [], [], "Surface plot — scalar or vector field on boundaries. Default."),
    ("general", "vol",        "Volume",
     1, 250, [], [], [], "Volume plot — 3D interior rendering."),
    ("general", "line",       "Line",
     1, 200, [], [], [], "Line plot — 1D plot group for sectional or parametric outputs."),
    ("general", "global",     "Global",
     1, 180, [], [], [], "Global plot — scalar outputs vs. time or sweep parameter."),
    ("general", "probe_dom",  "DomainProbe",
     1, 100, [], [], [], "Domain probe — time-history of a volume-integrated scalar."),
    ("general", "probe_bnd",  "BoundaryProbe",
     1, 90,  [], [], [], "Boundary probe — time-history of a surface-integrated scalar."),
    ("general", "probe_pnt",  "PointProbe",
     1, 80,  [], [], [], "Point probe — time-history of a scalar at a fixed point."),
    ("general", "contour",    "Contour",
     2, 70,  [], [], [], "Contour plot — iso-value lines / surfaces."),
    ("general", "arrow",      "Arrow",
     2, 50,  [], [], [], "Arrow plot — vector field glyphs."),
    ("general", "streamline", "Streamline",
     2, 30,  [], [], [], "Streamline plot — integral curves of a vector field."),
]

# intent-keyword → canonical-domain. Seed is deliberately narrow; unknown
# keywords resolve to None and produce no preamble injection.
_SYNONYMS_SEED = {
    # heat
    "heat": "heat",
    "thermal": "heat",
    "temperature": "heat",
    "conduction": "heat",
    "cooling": "heat",
    "heating": "heat",
    # flow
    "flow": "flow",
    "fluid": "flow",
    "velocity": "flow",
    "laminar": "flow",
    "turbulent": "flow",
    "pipe": "flow",
    # stress / structural
    "stress": "stress",
    "strain": "stress",
    "displacement": "stress",
    "elasticity": "stress",
    "mechanical": "stress",
    "structural": "stress",
    "deformation": "stress",
    "vibration": "stress",
    "modal": "stress",
    # low-freq em
    "magnetic": "em_low",
    "magnetics": "em_low",
    "inductance": "em_low",
    "electric-current": "em_low",
    "current": "em_low",
    "coil": "em_low",
    # high-freq em
    "wave": "em_high",
    "waves": "em_high",
    "rf": "em_high",
    "microwave": "em_high",
    "antenna": "em_high",
    "optics": "em_high",
    "photonic": "em_high",
    "scattering": "em_high",
    # acoustics
    "acoustic": "acoustics",
    "acoustics": "acoustics",
    "sound": "acoustics",
    "noise": "acoustics",
    # transport
    "diffusion": "transport",
    "species": "transport",
    "concentration": "transport",
    "solute": "transport",
    # chemistry
    "chemistry": "chem",
    "reaction": "chem",
    "kinetics": "chem",
    "reactor": "chem",
    # explicit custom-pde
    "general-pde": "custom",
    "custom-pde": "custom",
    "pde": "custom",
    "weak-form": "custom",
    "coefficient-form": "custom",
}


def seed_default_catalog(conn: sqlite3.Connection) -> None:
    """Seed the catalog with the hand-curated starter set.

    Idempotent — uses INSERT OR IGNORE via upsert_*. Safe to call on
    every init_db(); existing rows (including corpus_freq bumps from the
    promotion job) are preserved.
    """
    for row in _PHYSICS_SEED:
        (domain, tag, cls, rank, freq,
         studies, plots, auto, notes) = row
        upsert_interface(
            conn,
            domain_keyword=domain, stage="physics", tag_prefix=tag,
            class_name=cls, setup_cost_rank=rank, corpus_freq=freq,
            default_studies=studies, default_plots=plots,
            auto_features=auto, notes=notes,
        )
    for row in _STUDIES_SEED:
        (domain, tag, cls, rank, freq, _s, _p, _a, notes) = row
        upsert_interface(
            conn,
            domain_keyword=domain, stage="study", tag_prefix=tag,
            class_name=cls, setup_cost_rank=rank, corpus_freq=freq,
            notes=notes,
        )
    for row in _PLOTS_SEED:
        (domain, tag, cls, rank, freq, _s, _p, _a, notes) = row
        upsert_interface(
            conn,
            domain_keyword=domain, stage="result", tag_prefix=tag,
            class_name=cls, setup_cost_rank=rank, corpus_freq=freq,
            notes=notes,
        )
    for intent_kw, dom in _SYNONYMS_SEED.items():
        upsert_synonym(conn, intent_keyword=intent_kw, domain_keyword=dom)
    conn.commit()
