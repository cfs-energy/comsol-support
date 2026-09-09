"""Reference Manual property-key scraper for COMSOL Programming Reference Manual.

Parses pdftotext output to extract feature→property-key mappings from
COMSOL's Programming Reference Manual PDF. Populates the knowledge table
with property keys that the Javadoc omits.
"""

import logging
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("comsol_support.refmanual")


@dataclass
class PropertyRecord:
    """A single property extracted from a Reference Manual table."""
    feature_name: str       # e.g. "Block", "Stationary", "Cylinder"
    property_key: str       # e.g. "size", "pos", "reltol"
    value_type: str         # e.g. "double[]", "String", "on | off"
    default_value: str      # e.g. "{1,1,1}", "0", ""
    description: str        # from table row
    table_id: str           # e.g. "3-31" (for provenance)
    stage: str | None = None


@dataclass
class ScrapeResult:
    """Scraping outcome summary."""
    tables_found: int = 0
    records_extracted: int = 0
    rows_inserted: int = 0
    lines_skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ── Feature → stage classification ────────────────────────────────────────

# Import shared prefix map from javadoc_scraper
from comsol_support.javadoc_scraper import STAGE_MAP_PREFIX

# Additional mappings for Reference Manual feature names that don't
# appear in the Javadoc (these are create-type strings, not class names).
_REFMANUAL_STAGE_MAP = {
    # Geometry primitives
    "Block": "geometry", "Cylinder": "geometry", "Sphere": "geometry",
    "Cone": "geometry", "Ellipsoid": "geometry", "Hexahedron": "geometry",
    "Pyramid": "geometry", "Helix": "geometry", "ECone": "geometry",
    "Rectangle": "geometry", "Square": "geometry", "Circle": "geometry",
    "Ellipse": "geometry", "Interval": "geometry", "Point": "geometry",
    "Polygon": "geometry", "LineSegment": "geometry",
    "BezierPolygon": "geometry", "CubicBezier": "geometry",
    "QuadraticBezier": "geometry", "CircularArc": "geometry",
    "ParametricCurve": "geometry", "ParametricSurface": "geometry",
    # Geometry operations
    "Union": "geometry", "Difference": "geometry", "Intersection": "geometry",
    "Compose": "geometry", "Partition": "geometry", "Array": "geometry",
    "Copy": "geometry", "Mirror": "geometry", "Move": "geometry",
    "Rotate": "geometry", "Scale": "geometry", "Fillet": "geometry",
    "Chamfer": "geometry", "Extrude": "geometry", "Revolve": "geometry",
    "Sweep": "geometry", "WorkPlane": "geometry", "CrossSection": "geometry",
    "Import": "geometry", "FromMesh": "geometry",
    "PartitionDomains": "geometry", "PartitionEdges": "geometry",
    "PartitionFaces": "geometry",
    # Mesh
    "FreeTri": "mesh", "FreeQuad": "mesh", "FreeTet": "mesh",
    "Map": "mesh", "Swept": "mesh", "BndLayer": "mesh",
    "Refine": "mesh", "Convert": "mesh", "Adapt": "mesh",
    "Size": "mesh", "Distribution": "mesh", "EdgeGroup": "mesh",
    "BndLayerProp": "mesh",
    # Solvers / Studies
    "Stationary": "studies", "TimeDependent": "studies",
    "Eigenvalue": "studies", "Parametric": "studies",
    "FullyCoupled": "studies", "Segregated": "studies",
    "SegregatedStep": "studies", "Sensitivity": "studies",
    "Optimization": "studies", "Modal": "studies",
    "AWE": "studies", "FFT": "studies",
    # Results / Postprocessing
    "PlotGroup": "postprocessing", "Surface": "postprocessing",
    "Volume": "postprocessing", "Contour": "postprocessing",
    "Arrow": "postprocessing", "Streamline": "postprocessing",
    "Line": "postprocessing", "PointEval": "postprocessing",
    "GlobalEval": "postprocessing", "IntegrationEval": "postprocessing",
    "Table": "postprocessing", "Export": "postprocessing",
    # Materials
    "Material": "materials",
    # Parameters
    "Parameters": "parameters",
    # Functions
    "Interpolation": "functions", "Analytic": "functions",
    "Piecewise": "functions", "Step": "functions",
    "Rectangle_func": "functions", "Triangle": "functions",
    "GaussianPulse": "functions", "Ramp": "functions",
    "Random": "functions", "Wave": "functions",
}


def classify_feature_stage(feature_name: str) -> str | None:
    """Map a Reference Manual feature name to a pipeline stage.

    Tries direct lookup first (case-sensitive), then case-insensitive
    lookup (handles ALLCAPS→Titlecase normalization from PDF titles),
    then falls back to prefix matching using the shared Javadoc
    STAGE_MAP_PREFIX.
    """
    if feature_name in _REFMANUAL_STAGE_MAP:
        return _REFMANUAL_STAGE_MAP[feature_name]

    # Case-insensitive fallback for normalized ALLCAPS names
    # (e.g., "Freetri" from PDF title "FREETRI" should match "FreeTri")
    feature_lower = feature_name.lower()
    for key, stage in _REFMANUAL_STAGE_MAP.items():
        if key.lower() == feature_lower:
            return stage

    # Fall back to prefix matching from javadoc_scraper
    for prefix, stage in STAGE_MAP_PREFIX:
        if feature_name.startswith(prefix):
            return stage

    return None


# ── PDF text extraction ───────────────────────────────────────────────────

def check_pdftotext() -> str | None:
    """Check if pdftotext is available. Returns path or None."""
    return shutil.which("pdftotext")


def extract_pdf_text(pdf_path: Path, *, timeout: int = 120) -> str:
    """Run pdftotext on the PDF, return full text content.

    Uses -layout flag to preserve column alignment, which is critical
    for parsing tabular property data.
    """
    pdftotext_path = check_pdftotext()
    if not pdftotext_path:
        raise RuntimeError(
            "pdftotext (Poppler) is required for Reference Manual "
            "extraction and must be on PATH. Install with: "
            "apt install poppler-utils (Linux), brew install poppler "
            "(macOS), or winget install oschwartz10612.Poppler (Windows)."
        )

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    result = subprocess.run(
        # -enc UTF-8 pins pdftotext's output encoding; the matching
        # encoding= pins Python's decode (Windows would otherwise use
        # the legacy locale codepage and mangle symbols like Ω or °).
        [pdftotext_path, "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed (rc={result.returncode}): {result.stderr}")

    return result.stdout


# ── Table parsing ─────────────────────────────────────────────────────────

# Regex: TABLE N-M: TITLE
_TABLE_HEADER_RE = re.compile(
    r"^\s*TABLE\s+(\d+-\d+):\s*(.*)",
    re.IGNORECASE,
)

# Regex: column header line with PROPERTY (or NAME) and VALUE/VALUES/TYPE
_COL_HEADER_RE = re.compile(
    r"^\s*(PROPERTY|NAME)\s+(VALUES?|TYPE)\s",
    re.IGNORECASE,
)

# Feature section: "The Block Feature" or just "Block" as a standalone heading
_FEATURE_HEADING_RE = re.compile(
    r"^(?:The\s+)?(\w+(?:\s+\w+)?)\s*$"
)


def _detect_columns(header_line: str) -> list[int]:
    """Detect column start positions from a header line like
    'PROPERTY          VALUE           DEFAULT     DESCRIPTION'

    Returns list of start indices for each column.
    """
    cols = []
    tokens = re.finditer(r"\S+", header_line)
    for m in tokens:
        cols.append(m.start())
    return cols


# Words that should never be extracted as feature names from table titles
_TITLE_NOISE = frozenset({
    "THE", "A", "AN", "ALL", "VALID", "GENERAL", "COMMON", "ADDITIONAL",
    "AVAILABLE", "SUPPORTED", "DEFAULT", "VALUES", "PAIRS", "CUSTOM",
    "VARIOUS", "SPECIAL", "SPECIFIC",
})


def _extract_feature_from_table_title(title: str) -> str:
    """Extract feature name from table title.

    Examples:
        'VALID PROPERTY/VALUE PAIRS FOR BLOCK.' → 'Block'
        'PROPERTIES FOR STATIONARY.' → 'Stationary'
        'VALID PROPERTY/VALUE PAIRS FOR CYLINDER.' → 'Cylinder'
        'PROPERTY FOR ROTATINGBOUNDARY.' → 'RotatingBoundary'
        'VALID GENERAL PROPERTY/VALUE PAIRS FOR ADVANCED.' → 'Advanced'
        'PROPERTIES FOR THE SURROGATEMODELGEOMETRYSAMPLING FEATURE.' → 'Surrogatemodelgeometrysampling'
    """
    # Pattern: "... FOR [THE] <FEATURENAME> [FEATURE]."
    # Try to match the last meaningful word before trailing FEATURE/period
    m = re.search(
        r"FOR\s+(?:THE\s+)?(\w+)(?:\s+FEATURE)?\.?\s*$",
        title, re.IGNORECASE,
    )
    if m:
        raw = m.group(1).upper()
        if raw not in _TITLE_NOISE and len(raw) > 1:
            return _normalize_feature_name(m.group(1))

    # Pattern: "PROPERTIES FOR <NAME>" anywhere in the title
    m = re.search(r"FOR\s+(?:THE\s+)?(\w+)", title, re.IGNORECASE)
    if m:
        raw = m.group(1).upper()
        if raw not in _TITLE_NOISE and len(raw) > 1:
            return _normalize_feature_name(m.group(1))

    # Pattern: "VALID <NAME> PROPERTIES" or "<NAME> PROPERTIES."
    m = re.search(
        r"(?:VALID\s+)?(\w+(?:\s+\w+)?)\s+PROPERT",
        title, re.IGNORECASE,
    )
    if m:
        # Take the last word before PROPERT as the feature name
        words = m.group(1).strip().split()
        # Filter noise words
        candidates = [w for w in words if w.upper() not in _TITLE_NOISE
                       and w.upper() not in {"PROPERTY", "OPTIONAL", "GENERAL"}]
        if candidates:
            raw = candidates[-1]
            return _normalize_feature_name(raw)

    return ""


def _normalize_feature_name(raw: str) -> str:
    """Convert ALLCAPS to TitleCase, preserve mixed case."""
    if raw.isupper() and len(raw) > 2:
        # CamelCase-like: ROTATINGBOUNDARY → Rotatingboundary
        return raw.title()
    return raw


def _find_feature_context(lines: list[str], table_line_idx: int) -> str:
    """Look backwards from a table to find the feature name from context.

    Searches for patterns like:
        - Standalone feature name heading (e.g. "Block")
        - "The Block Feature"
        - model.component(...).geom(...).create(..., "Block")
    """
    search_start = max(0, table_line_idx - 40)
    for i in range(table_line_idx - 1, search_start - 1, -1):
        line = lines[i].strip()
        # Match create() call
        m = re.search(r'\.create\([^,]*,\s*"(\w+)"\)', line)
        if m:
            return m.group(1)
    return ""


def parse_property_tables(text: str) -> list[PropertyRecord]:
    """Find and parse all property tables from pdftotext output.

    Returns a list of PropertyRecord for each valid property row found.
    """
    lines = text.split("\n")
    records: list[PropertyRecord] = []
    skipped = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        # Look for TABLE header
        m = _TABLE_HEADER_RE.match(line)
        if not m:
            i += 1
            continue

        table_id = m.group(1)
        table_title = m.group(2).strip()

        # Only process property/value tables
        title_upper = table_title.upper()
        if not any(kw in title_upper for kw in ["PROPERT", "VALUE PAIR"]):
            i += 1
            continue

        # Extract feature name from title
        feature_name = _extract_feature_from_table_title(table_title)

        # If not found in title, search context above
        if not feature_name:
            feature_name = _find_feature_context(lines, i)

        # Skip to column header line
        j = i + 1
        col_positions = None
        while j < min(i + 10, len(lines)):
            if _COL_HEADER_RE.match(lines[j]):
                col_positions = _detect_columns(lines[j])
                j += 1  # skip blank line after header
                break
            j += 1

        if col_positions is None or len(col_positions) < 2:
            i = j
            continue

        # Now parse property rows until we hit the end of the table
        j += 1  # skip blank line after column header
        while j < len(lines):
            row_line = lines[j]
            stripped = row_line.strip()

            # End of table: blank line followed by non-property content,
            # another TABLE header, page footer, EXAMPLE/SYNTAX/SEE ALSO
            if not stripped:
                # Check if next non-blank line is a continuation or new content
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k >= len(lines):
                    break
                next_line = lines[k].strip()
                # If next content starts with a property-like word, continue
                # If it's a TABLE header, section heading, or prose, stop
                if (_TABLE_HEADER_RE.match(next_line) or
                    next_line.startswith("EXAMPLE") or
                    next_line.startswith("SYNTAX") or
                    next_line.startswith("SEE ALSO") or
                    next_line.startswith("DESCRIPTION") or
                    next_line.startswith("COMPATIBILITY") or
                    next_line.startswith("Code for Use") or
                    "CHAPTER" in next_line or
                    next_line.startswith("The ") or
                    "|" in next_line and next_line[0].isupper() and len(next_line.split()) > 8):
                    break
                # Check if it looks like a property row (starts with lowercase word at col 0 area)
                if (len(col_positions) >= 1 and
                    k < len(lines) and
                    _is_property_row(lines[k], col_positions)):
                    j = k
                    continue
                break
            j += 1

            # Skip page footers and headers
            if ("CHAPTER" in stripped or
                "GEOMETRY COMMANDS" in stripped or
                "ABOUT GENERAL COMMANDS" in stripped or
                "SOLVERS AND STUDY STEPS" in stripped or
                "RESULTS" in stripped and "|" in stripped or
                stripped.startswith("\x0c")):
                continue

            # Try to parse as property row
            record = _parse_property_row(row_line, col_positions, feature_name, table_id)
            if record:
                records.append(record)
            else:
                # Could be a continuation line or non-property line
                if stripped and not stripped[0].islower() and not stripped[0] == '"':
                    skipped += 1

        i = j

    logger.info("Parsed %d property records, skipped %d ambiguous lines",
                len(records), skipped)
    return records


# Common English words that are NOT property names — continuation text
_NOISE_WORDS = frozenset({
    "the", "this", "that", "and", "for", "with", "from", "are", "not",
    "can", "all", "has", "its", "set", "use", "see", "also", "when",
    "where", "which", "each", "any", "only", "than", "used", "using",
    "other", "between", "into", "upon", "about", "will", "should",
    "must", "does", "have", "been", "being", "such", "same", "both",
    "here", "there", "then", "thus", "some", "more",
})


def _is_property_row(line: str, col_positions: list[int]) -> bool:
    """Check if a line looks like a property row.

    A property row starts with a lowercase identifier near the first column.
    Filters out common English words that appear in continuation text.
    """
    stripped = line.lstrip()
    if not stripped:
        return False

    # Property names are lowercase identifiers (camelCase or alllower)
    m = re.match(r"[a-z]\w*", stripped)
    if not m:
        return False

    candidate = m.group(0)

    # Filter out common English words (continuation text)
    if candidate in _NOISE_WORDS:
        return False

    # The property name should start near the first column position
    indent = len(line) - len(stripped)
    if col_positions and abs(indent - col_positions[0]) > 8:
        return False

    # Must have content in at least one more column (value or description)
    remaining = stripped[m.end():]
    if len(remaining.strip()) < 2:
        return False

    # Property names are typically camelCase or short lowercase — not
    # multi-word prose. If the identifier is followed immediately by a
    # space and more lowercase words, it might be prose.
    # Real property rows have whitespace gap then value type.
    if remaining and not re.match(r"\s{2,}", remaining):
        return False

    return True


def _parse_property_row(
    line: str,
    col_positions: list[int],
    feature_name: str,
    table_id: str,
) -> PropertyRecord | None:
    """Parse a single property table row into a PropertyRecord.

    Uses column positions detected from the header to split fields.
    """
    if not line.strip():
        return None

    # Property names start with a lowercase letter
    stripped = line.lstrip()
    m = re.match(r"([a-z]\w*)", stripped)
    if not m:
        return None

    property_key = m.group(1)

    # Filter out common English words (continuation text)
    if property_key in _NOISE_WORDS:
        return None

    # Check indentation — property key should be near first column
    indent = len(line) - len(stripped)
    if col_positions and abs(indent - col_positions[0]) > 8:
        return None

    # Property rows have a multi-space gap between key and value
    remaining = stripped[m.end():]
    if remaining and not re.match(r"\s{2,}", remaining):
        return None

    # Split remaining text using column positions
    # Typical columns: PROPERTY, VALUE, DEFAULT, DESCRIPTION
    # Sometimes: PROPERTY, VALUE, DESCRIPTION (no default)
    # Sometimes: NAME, VALUE, DEFAULT, DESCRIPTION
    # Sometimes: NAME, TYPE, DESCRIPTION

    num_cols = len(col_positions)
    if num_cols < 2:
        return None

    # Extract fields using column positions
    fields = []
    for ci in range(1, num_cols):
        start = col_positions[ci]
        end = col_positions[ci + 1] if ci + 1 < num_cols else len(line)
        if start < len(line):
            fields.append(line[start:end].strip())
        else:
            fields.append("")

    # Map fields based on column count
    if num_cols >= 4:
        # PROPERTY | VALUE | DEFAULT | DESCRIPTION
        value_type = fields[0] if len(fields) > 0 else ""
        default_value = fields[1] if len(fields) > 1 else ""
        description = fields[2] if len(fields) > 2 else ""
    elif num_cols == 3:
        # PROPERTY | VALUE | DESCRIPTION (no default)
        value_type = fields[0] if len(fields) > 0 else ""
        default_value = ""
        description = fields[1] if len(fields) > 1 else ""
    else:
        # PROPERTY | VALUE
        value_type = fields[0] if len(fields) > 0 else ""
        default_value = ""
        description = ""

    # Skip rows where value_type is empty (likely a continuation or section text)
    if not value_type:
        return None

    return PropertyRecord(
        feature_name=feature_name,
        property_key=property_key,
        value_type=value_type,
        default_value=default_value,
        description=description,
        table_id=table_id,
    )


# ── Knowledge table ingestion ─────────────────────────────────────────────

def ingest_properties(
    conn: sqlite3.Connection,
    records: list[PropertyRecord],
    *,
    source_tag: str = "refmanual-6.4",
) -> int:
    """Write PropertyRecords to knowledge table. Returns row count."""
    from comsol_support.db import clear_knowledge_by_source, store_knowledge_batch

    clear_knowledge_by_source(conn, source_tag)

    rows: list[tuple] = []
    for rec in records:
        stage = rec.stage or classify_feature_stage(rec.feature_name)
        desc = rec.description
        if rec.default_value:
            desc = f"{desc} [default: {rec.default_value}]" if desc else f"[default: {rec.default_value}]"

        rows.append((
            rec.feature_name,            # class
            "set",                        # method (all properties use .set())
            f'set("{rec.property_key}", value)',  # signature
            rec.property_key,            # property_key
            rec.value_type,              # value_type
            stage,                        # stage
            None,                         # module
            source_tag,                   # source
            desc,                         # description
        ))

    if rows:
        return store_knowledge_batch(conn, rows)
    return 0


# ── Main entry point ──────────────────────────────────────────────────────

def scrape_reference_manual(
    pdf_path: Path,
    conn: sqlite3.Connection,
    *,
    source_tag: str = "refmanual-6.4",
) -> ScrapeResult:
    """Main entry point. Extract text, parse property tables, populate knowledge."""
    result = ScrapeResult()

    try:
        text = extract_pdf_text(pdf_path)
    except (RuntimeError, FileNotFoundError) as e:
        result.errors.append(str(e))
        return result

    records = parse_property_tables(text)
    result.tables_found = len(set(r.table_id for r in records))
    result.records_extracted = len(records)

    # Classify stages
    for rec in records:
        rec.stage = classify_feature_stage(rec.feature_name)

    result.rows_inserted = ingest_properties(conn, records, source_tag=source_tag)
    return result


def scrape_from_text(
    text: str,
    conn: sqlite3.Connection,
    *,
    source_tag: str = "refmanual-6.4",
) -> ScrapeResult:
    """Scrape from already-extracted text (for testing without pdftotext)."""
    result = ScrapeResult()

    records = parse_property_tables(text)
    result.tables_found = len(set(r.table_id for r in records))
    result.records_extracted = len(records)

    for rec in records:
        rec.stage = classify_feature_stage(rec.feature_name)

    result.rows_inserted = ingest_properties(conn, records, source_tag=source_tag)
    return result


def stage_distribution(
    conn: sqlite3.Connection,
    source_tag: str = "refmanual-6.4",
) -> dict[str, int]:
    """Get stage distribution of knowledge rows for this source."""
    rows = conn.execute(
        "SELECT COALESCE(stage, 'untagged') as s, COUNT(*) as c "
        "FROM knowledge WHERE source = ? GROUP BY s ORDER BY c DESC",
        (source_tag,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
