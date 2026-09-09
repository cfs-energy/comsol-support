"""Javadoc ontology scraper for COMSOL API documentation.

Parses Javadoc HTML files from the COMSOL installation, extracts
class/method/signature data, and populates the knowledge table.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


@dataclass
class MethodInfo:
    name: str
    signature: str
    return_type: str
    description: str
    deprecated: bool = False


@dataclass
class ClassRecord:
    name: str
    package: str
    kind: str  # "interface" | "class" | "enum"
    description: str
    superinterfaces: list[str] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)
    stage: str | None = None
    source_path: str = ""


@dataclass
class ScrapeResult:
    classes_scraped: int = 0
    methods_extracted: int = 0
    rows_inserted: int = 0
    errors: list[str] = field(default_factory=list)


STAGE_MAP_PACKAGE = {
    "com.comsol.model.physics": "physics",
}

STAGE_MAP_PREFIX = [
    ("Geom", "geometry"),
    ("Selection", "selections"),
    ("Material", "materials"),
    ("Mesh", "mesh"),
    ("Study", "studies"),
    ("Solver", "studies"),
    ("Result", "postprocessing"),
    ("Report", "postprocessing"),
    ("Numerical", "postprocessing"),
    ("Evaluation", "postprocessing"),
    ("Table", "postprocessing"),
    ("Export", "postprocessing"),
    ("Dataset", "postprocessing"),
    ("Param", "parameters"),
    ("ModelParam", "parameters"),
    ("Func", "functions"),
    ("Function", "functions"),
    ("Cpl", "functions"),
    ("Batch", "studies"),
    ("Opt", "studies"),
    ("Physics", "physics"),
    ("Constr", "physics"),
    ("Weak", "physics"),
    ("Init", "physics"),
    ("Ode", "physics"),
    ("Field", "physics"),
    ("Frame", "physics"),
    ("Coordsys", "geometry"),
    ("Coors", "geometry"),
    ("Unit", "parameters"),
    ("View", "postprocessing"),
    ("ColorTable", "postprocessing"),
    ("HideDraw", "postprocessing"),
    ("HideGeom", "geometry"),
    ("HideMesh", "mesh"),
    ("Pair", "geometry"),
    ("WorkPlane", "geometry"),
]


def classify_stage(class_name: str, package: str) -> str | None:
    if package in STAGE_MAP_PACKAGE:
        return STAGE_MAP_PACKAGE[package]
    for prefix, stage in STAGE_MAP_PREFIX:
        if class_name.startswith(prefix):
            return stage
    return None


def load_class_index(api_dir: Path) -> list[dict]:
    index_path = api_dir / "type-search-index.js"
    text = index_path.read_text(encoding="utf-8")

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array found in {index_path}")

    entries = json.loads(match.group())
    results = []
    for entry in entries:
        pkg = entry.get("p", "")
        name = entry.get("l", "")
        if not pkg or not name:
            continue
        rel_path = pkg.replace(".", "/") + "/" + name + ".html"
        results.append({"package": pkg, "name": name, "rel_path": rel_path})
    return results


class _MethodTableParser(HTMLParser):
    """State-machine parser for Javadoc method summary tables."""

    def __init__(self):
        super().__init__()
        self.methods: list[MethodInfo] = []
        self.superinterfaces: list[str] = []
        self.class_name: str = ""
        self.class_kind: str = "interface"
        self.class_description: str = ""

        self._in_member_summary = False
        self._in_row = False
        self._current_col: str | None = None
        self._col_text: list[str] = []
        self._row_data: dict = {}
        self._depth = 0

        self._in_type_name_label = False
        self._in_pre_block = False
        self._pre_text: list[str] = []

        self._in_superinterfaces_dt = False
        self._in_superinterfaces_dd = False
        self._in_dd_link = False
        self._dd_link_text: list[str] = []

        self._in_class_desc = False
        self._class_desc_depth = 0
        self._class_desc_text: list[str] = []
        self._found_class_desc = False

        self._in_deprecated_label = False
        self._row_has_deprecated = False

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)

        if tag == "table":
            cls = attr_dict.get("class", "")
            if "memberSummary" in cls:
                self._in_member_summary = True
            return

        if tag == "span" and attr_dict.get("class") == "typeNameLabel":
            self._in_type_name_label = True
            return

        if tag == "pre" and not self.class_name:
            self._in_pre_block = True
            self._pre_text = []
            return

        if tag == "dt":
            self._in_superinterfaces_dt = True
            self._dt_text: list[str] = []
            return

        if tag == "dd" and self._in_superinterfaces_dt:
            self._in_superinterfaces_dd = True
            self._in_superinterfaces_dt = False
            return

        if tag == "a" and self._in_superinterfaces_dd:
            self._in_dd_link = True
            self._dd_link_text = []
            return

        if self._in_member_summary:
            if tag == "tr":
                self._in_row = True
                self._row_data = {}
                self._row_has_deprecated = False
                self._row_is_data = False
                return

            if self._in_row:
                col_class = attr_dict.get("class", "")
                if "colFirst" in col_class and tag == "td":
                    self._current_col = "return_type"
                    self._col_text = []
                    self._row_is_data = True
                elif "colSecond" in col_class and getattr(self, "_row_is_data", False):
                    self._current_col = "signature"
                    self._col_text = []
                elif "colLast" in col_class and tag == "td" and getattr(self, "_row_is_data", False):
                    self._current_col = "description"
                    self._col_text = []

                if tag == "span" and attr_dict.get("class") == "deprecatedLabel":
                    self._row_has_deprecated = True

        if (tag == "div" and attr_dict.get("class") == "block"
                and self.class_name and not self._found_class_desc
                and not self._in_member_summary):
            self._in_class_desc = True
            self._class_desc_depth = 1
            self._class_desc_text = []
            return

        if self._in_class_desc and tag == "div":
            self._class_desc_depth += 1

    def handle_endtag(self, tag):
        if tag == "span" and self._in_type_name_label:
            self._in_type_name_label = False
            return

        if tag == "pre" and self._in_pre_block:
            self._in_pre_block = False
            pre_content = " ".join("".join(self._pre_text).split())
            if "interface " in pre_content:
                self.class_kind = "interface"
            elif "enum " in pre_content:
                self.class_kind = "enum"
            else:
                self.class_kind = "class"
            return

        if tag == "dt" and self._in_superinterfaces_dt:
            dt_text = "".join(getattr(self, "_dt_text", []))
            if "Superinterfaces" not in dt_text:
                self._in_superinterfaces_dt = False
            return

        if tag == "dd" and self._in_superinterfaces_dd:
            self._in_superinterfaces_dd = False
            return

        if tag == "a" and self._in_dd_link:
            self._in_dd_link = False
            link_text = "".join(self._dd_link_text).strip()
            if link_text:
                self.superinterfaces.append(link_text)
            return

        if self._in_class_desc and tag == "div":
            self._class_desc_depth -= 1
            if self._class_desc_depth <= 0:
                self._in_class_desc = False
                self._found_class_desc = True
                self.class_description = _clean_text(
                    "".join(self._class_desc_text)
                )
            return

        if not self._in_member_summary:
            return

        if tag == "table":
            self._in_member_summary = False
            return

        if tag == "tr" and self._in_row:
            self._in_row = False
            if "return_type" in self._row_data and "signature" in self._row_data:
                raw_sig = self._row_data["signature"]
                name, signature = _parse_method_signature(raw_sig)
                if name:
                    self.methods.append(MethodInfo(
                        name=name,
                        signature=signature,
                        return_type=_clean_text(self._row_data.get("return_type", "")),
                        description=_clean_text(self._row_data.get("description", "")),
                        deprecated=self._row_has_deprecated,
                    ))
            self._current_col = None
            return

        if self._in_row and self._current_col and tag in ("td", "th"):
            text = "".join(self._col_text)
            self._row_data[self._current_col] = text
            self._current_col = None

    def handle_data(self, data):
        if self._in_type_name_label:
            self.class_name = data.strip()
            return

        if self._in_pre_block:
            self._pre_text.append(data)
            return

        if self._in_superinterfaces_dt:
            self._dt_text.append(data)
            return

        if self._in_dd_link:
            self._dd_link_text.append(data)
            return

        if self._in_class_desc:
            self._class_desc_text.append(data)
            return

        if self._current_col is not None:
            self._col_text.append(data)

    def handle_entityref(self, name):
        char = {"nbsp": " ", "gt": ">", "lt": "<", "amp": "&"}.get(name, "")
        if self._current_col is not None:
            self._col_text.append(char)
        if self._in_class_desc:
            self._class_desc_text.append(char)

    def handle_charref(self, name):
        if name == "8203":  # zero-width space
            return
        try:
            char = chr(int(name))
        except ValueError:
            char = ""
        if self._current_col is not None:
            self._col_text.append(char)
        if self._in_class_desc:
            self._class_desc_text.append(char)


def _parse_method_signature(raw: str) -> tuple[str, str]:
    text = _clean_text(raw)
    match = re.match(r"(\w+)\s*(\(.*\))?", text)
    if not match:
        return ("", "")
    name = match.group(1)
    params = match.group(2) or "()"
    return name, f"{name}{params}"


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("\u200b", "")
    return text


def parse_class_page(html_path: Path) -> ClassRecord | None:
    try:
        html = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    parser = _MethodTableParser()
    parser.feed(html)

    if not parser.class_name:
        return None

    return ClassRecord(
        name=parser.class_name,
        package="",  # filled by caller
        kind=parser.class_kind,
        description=parser.class_description,
        superinterfaces=parser.superinterfaces,
        methods=parser.methods,
        source_path=str(html_path),
    )


def scrape_javadoc(
    api_dir: Path,
    conn: sqlite3.Connection,
    *,
    source_tag: str = "javadoc-6.4",
) -> ScrapeResult:
    from comsol_support.db import clear_knowledge_by_source, store_knowledge_batch

    result = ScrapeResult()
    index = load_class_index(api_dir)

    clear_knowledge_by_source(conn, source_tag)

    rows: list[tuple] = []

    for entry in index:
        html_path = api_dir / entry["rel_path"]
        if not html_path.exists():
            result.errors.append(f"Missing: {entry['rel_path']}")
            continue

        record = parse_class_page(html_path)
        if not record:
            result.errors.append(f"Parse failed: {entry['rel_path']}")
            continue

        record.package = entry["package"]
        record.stage = classify_stage(record.name, record.package)
        result.classes_scraped += 1

        supers_str = ", ".join(record.superinterfaces) if record.superinterfaces else ""
        class_desc = record.description
        if supers_str:
            class_desc = f"{class_desc} [extends: {supers_str}]" if class_desc else f"[extends: {supers_str}]"

        rows.append((
            record.name,     # class
            None,             # method
            None,             # signature
            None,             # property_key
            record.kind,      # value_type
            record.stage,     # stage
            None,             # module
            source_tag,       # source
            class_desc,       # description
        ))

        for method in record.methods:
            result.methods_extracted += 1
            rows.append((
                record.name,
                method.name,
                method.signature,
                None,
                method.return_type,
                record.stage,
                None,
                source_tag,
                method.description,
            ))

    result.rows_inserted = store_knowledge_batch(conn, rows)
    return result


def stage_distribution(conn: sqlite3.Connection, source_tag: str = "javadoc-6.4") -> dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(stage, 'untagged') as s, COUNT(*) as c "
        "FROM knowledge WHERE source = ? GROUP BY s ORDER BY c DESC",
        (source_tag,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
