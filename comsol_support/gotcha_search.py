"""gotcha-search — symptom-indexed retrieval over `docs/known-gotchas.md`.

`docs/known-gotchas.md` is the living catalog of confirmed COMSOL 6.4
+ headless-solve pitfalls. Each entry is anchored by a stable
`G-DOMAIN-PHENOMENON` ID and follows a fixed schema (Symptom / Root
cause / Workaround / Observed in). This CLI greps that file by
keyword or regex and returns whole matching entries.

Future campaigns hitting a recurring symptom should run this *first*
before authoring a new harness variant.

Designed to be:
- Dependency-free (stdlib only, mirrors comsol-support's policy).
- Independent of the SQLite DB — the doc is the source of truth.
- Fail-soft when the doc is missing (returns empty + clear message,
  exits 2 — not 1, to distinguish from "ran but found nothing").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


GOTCHA_DOC = (
    Path(__file__).resolve().parent.parent / "docs" / "known-gotchas.md"
)

# Each entry begins at a heading line `## G-<...>` and runs to the next
# `## ` or the trailing comment marker. The header IS the entry ID.
_ENTRY_RE = re.compile(r"^## (G-[A-Z0-9-]+)\s*$", re.MULTILINE)


_SYMPTOM_RE = re.compile(
    r"\*\*Symptom\*\*:\s*(.+?)(?=\n\n|\n\*\*)", re.DOTALL,
)


@dataclass
class Gotcha:
    id: str
    body: str
    score: int = 0
    matched_fields: list[str] = field(default_factory=list)

    def symptom_line(self) -> str:
        """The entry's Symptom text (single-spaced), or '' if absent."""
        m = _SYMPTOM_RE.search(self.body)
        return " ".join(m.group(1).split()) if m else ""


def parse_gotchas(doc_path: Path = GOTCHA_DOC) -> list[Gotcha]:
    """Parse known-gotchas.md into discrete entries.

    Returns the list in document order. Empty list if the doc is
    missing or has no entries.
    """
    if not doc_path.exists():
        return []
    text = doc_path.read_text(encoding="utf-8", errors="replace")
    starts = [(m.group(1), m.start()) for m in _ENTRY_RE.finditer(text)]
    if not starts:
        return []
    out: list[Gotcha] = []
    for i, (gid, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = text[start:end].rstrip()
        # Trim the trailing HTML appender comment if it landed in the
        # last entry.
        body = re.sub(r"\n<!--.*?-->\s*$", "", body, flags=re.DOTALL)
        out.append(Gotcha(id=gid, body=body))
    return out


def search_gotchas(
    query: str,
    *,
    doc_path: Path = GOTCHA_DOC,
    use_regex: bool = False,
) -> list[Gotcha]:
    """Return entries matching `query`.

    Ranking: entries where the query matches the ID get rank 3,
    Symptom-line matches get rank 2, anywhere-in-body matches get
    rank 1. Results sorted by descending score, then by doc order.

    `use_regex=True` treats `query` as a regex (case-insensitive);
    otherwise it is a literal substring.
    """
    gotchas = parse_gotchas(doc_path)
    if not query.strip():
        return gotchas

    if use_regex:
        try:
            pat = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex {query!r}: {e}") from e

        def matches(s: str) -> bool:
            return pat.search(s) is not None
    else:
        needle = query.casefold()

        def matches(s: str) -> bool:
            return needle in s.casefold()

    hits: list[Gotcha] = []
    for g in gotchas:
        scored = 0
        fields: list[str] = []
        if matches(g.id):
            scored = max(scored, 3)
            fields.append("id")
        # Pull the Symptom line for a focused rank-2 check.
        symptom = g.symptom_line()
        if symptom and matches(symptom):
            scored = max(scored, 2)
            fields.append("symptom")
        if scored == 0 and matches(g.body):
            scored = 1
            fields.append("body")
        if scored > 0:
            g.score = scored
            g.matched_fields = fields
            hits.append(g)

    hits.sort(key=lambda x: (-x.score, gotchas.index(x)))
    return hits


# Tokens too generic to discriminate between gotcha entries — every
# COMSOL failure message contains several of these.
_SUGGEST_STOPWORDS = frozenset({
    "error", "errors", "failed", "failure", "exception", "unknown",
    "comsol", "model", "java", "study", "solver", "solve", "with",
    "this", "that", "from", "after", "while", "when", "file", "line",
    "null", "true", "false", "detail", "telemetry", "sidecar", "trace",
    "partial", "halt", "reason", "following", "feature", "problem",
    "encountered", "modelexporter", "modelutil",
})

# No dots in the token class: `ModelUtil.disconnect` must yield the
# components (which substring-match IDs and symptom lines), not the
# dotted compound (which matches nothing).
_SUGGEST_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")

# Cap the per-failure token fan-out; error messages repeat themselves and
# the first distinctive terms carry the signal.
_SUGGEST_MAX_TOKENS = 30


def suggest_for_error(
    error_text: str,
    *,
    doc_path: Path = GOTCHA_DOC,
    limit: int = 3,
) -> list[Gotcha]:
    """Best-effort gotcha suggestions for a failure message.

    Tokenizes the error text and unions per-token search hits, summing
    each entry's per-token scores — an entry corroborated by several
    tokens outranks one matched by a single (even id-level) token. Only
    id- and symptom-level matches (per-token score >= 2) count —
    body-level matches on single generic tokens are noise.
    Never raises; returns [] when the doc is missing or nothing matches.
    """
    try:
        if not error_text or not parse_gotchas(doc_path):
            return []
        tokens: list[str] = []
        seen_tok: set[str] = set()
        for m in _SUGGEST_TOKEN_RE.finditer(error_text):
            tok = m.group(0).casefold().strip(".-")
            if len(tok) < 4 or tok in _SUGGEST_STOPWORDS or tok in seen_tok:
                continue
            seen_tok.add(tok)
            tokens.append(tok)
            if len(tokens) >= _SUGGEST_MAX_TOKENS:
                break

        best: dict[str, Gotcha] = {}
        for tok in tokens:
            for g in search_gotchas(tok, doc_path=doc_path):
                if g.score < 2:
                    continue
                cur = best.get(g.id)
                if cur is None:
                    best[g.id] = g
                else:
                    # Sum scores across tokens; union the matched fields.
                    cur.score += g.score
                    for f in g.matched_fields:
                        if f not in cur.matched_fields:
                            cur.matched_fields.append(f)
        ranked = sorted(best.values(), key=lambda g: -g.score)
        return ranked[:limit]
    except Exception:  # noqa: BLE001 — advisory path must never raise
        return []


def cmd_gotcha_search(args: argparse.Namespace) -> int:
    try:
        hits = search_gotchas(
            args.query or "",
            doc_path=Path(args.doc) if args.doc else GOTCHA_DOC,
            use_regex=args.regex,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not GOTCHA_DOC.exists() and not args.doc:
        from comsol_support._source_checkout import INSTALL_HINT
        print(
            f"error: gotchas doc not found at {GOTCHA_DOC}. {INSTALL_HINT} "
            "(or pass --doc <path-to-known-gotchas.md>)",
            file=sys.stderr,
        )
        return 2

    if args.format == "json":
        print(json.dumps(
            [{"id": g.id, "score": g.score,
              "matched_fields": g.matched_fields, "body": g.body}
             for g in hits],
            indent=2,
        ))
        return 0

    if not hits:
        if args.query:
            print(f"No gotchas matched {args.query!r}.")
        else:
            print("No gotcha entries found in the doc.")
        return 0

    for i, g in enumerate(hits):
        if i:
            print("\n---\n")
        if args.ids_only:
            tag = ",".join(g.matched_fields)
            print(f"{g.id}  [match:{tag}]")
        else:
            print(g.body)
    return 0


def add_gotcha_search_subparser(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser(
        "gotcha-search",
        help="Search docs/known-gotchas.md by keyword or regex",
    )
    p.add_argument(
        "query", nargs="?", default="",
        help="Keyword or regex (with --regex). Empty query → all entries.",
    )
    p.add_argument(
        "--regex", action="store_true",
        help="Treat query as a case-insensitive Python regex",
    )
    p.add_argument(
        "--doc",
        help="Override path to known-gotchas.md (default: in-repo docs/)",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
    )
    p.add_argument(
        "--ids-only", action="store_true",
        help="Print only the matching IDs (one per line) — useful as "
             "an index for `comsol-support search` or follow-up reading",
    )
    p.set_defaults(func=cmd_gotcha_search)
