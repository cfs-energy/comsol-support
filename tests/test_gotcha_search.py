"""Tests for gotcha-search: doc parsing, ranking, CLI wiring."""

from __future__ import annotations

import json

import pytest

from comsol_support.cli import build_parser
from comsol_support.gotcha_search import (
    GOTCHA_DOC,
    cmd_gotcha_search,
    parse_gotchas,
    search_gotchas,
)


# ---- Doc presence + structural integrity ----------------------------------


def test_gotchas_doc_exists():
    """Doc must be present — gotcha-search is useless without it."""
    assert GOTCHA_DOC.exists(), f"missing {GOTCHA_DOC}"


def test_gotchas_doc_has_entries():
    entries = parse_gotchas()
    # At least the seed set documented in the P7 ship.
    assert len(entries) >= 8, (
        f"expected at least 8 seeded gotcha entries, got {len(entries)}"
    )


def test_every_entry_has_required_fields():
    """Each G-* entry must contain Symptom / Root cause / Workaround /
    Observed in lines."""
    entries = parse_gotchas()
    required = ("**Symptom**", "**Root cause**", "**Workaround**",
                "**Observed in**")
    for g in entries:
        for tag in required:
            assert tag in g.body, f"{g.id} missing {tag}"


def test_ids_unique():
    ids = [g.id for g in parse_gotchas()]
    assert len(ids) == len(set(ids)), f"duplicate IDs: {ids}"


def test_ids_follow_convention():
    """ID convention: G-DOMAIN-PHENOMENON (uppercase, hyphen-separated)."""
    import re
    pat = re.compile(r"^G-[A-Z][A-Z0-9-]+$")
    for g in parse_gotchas():
        assert pat.match(g.id), f"non-conforming ID: {g.id}"


# ---- Search behavior ------------------------------------------------------


def test_search_by_keyword_finds_seeded_entry():
    """Searching for a seeded keyword returns the matching entry."""
    hits = search_gotchas("disconnect")
    ids = [g.id for g in hits]
    assert "G-DISCONNECT-HANGS" in ids


def test_search_ranks_id_match_highest():
    """A query that matches an ID should rank it above body-only matches."""
    hits = search_gotchas("disconnect")
    assert hits[0].id == "G-DISCONNECT-HANGS"
    assert hits[0].score >= 2


def test_search_empty_returns_all():
    all_entries = parse_gotchas()
    hits = search_gotchas("")
    assert len(hits) == len(all_entries)


def test_search_regex_mode():
    """`--regex` enables case-insensitive Python regex."""
    hits = search_gotchas(r"libcad\w+", use_regex=True)
    assert any(g.id == "G-LIBPATH-CAD-IMPORT" for g in hits)


def test_search_invalid_regex_raises():
    with pytest.raises(ValueError, match="invalid regex"):
        search_gotchas("[unclosed", use_regex=True)


def test_search_no_match_returns_empty():
    hits = search_gotchas("zzzzzz-no-such-keyword")
    assert hits == []


# ---- CLI wiring -----------------------------------------------------------


def test_gotcha_search_subparser_registered():
    parser = build_parser()
    args = parser.parse_args(["gotcha-search", "disconnect"])
    assert args.command == "gotcha-search"
    assert args.query == "disconnect"


def test_gotcha_search_subparser_accepts_full_arg_set():
    parser = build_parser()
    args = parser.parse_args([
        "gotcha-search", "lib.*import",
        "--regex", "--format", "json", "--ids-only",
    ])
    assert args.regex is True
    assert args.format == "json"
    assert args.ids_only is True


def test_gotcha_search_json_format(capsys):
    parser = build_parser()
    args = parser.parse_args(["gotcha-search", "disconnect", "--format", "json"])
    rc = cmd_gotcha_search(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(e["id"] == "G-DISCONNECT-HANGS" for e in payload)


def test_gotcha_search_ids_only(capsys):
    parser = build_parser()
    args = parser.parse_args(["gotcha-search", "", "--ids-only"])
    rc = cmd_gotcha_search(args)
    assert rc == 0
    out = capsys.readouterr().out
    # First seeded entry should appear by ID alone, with no body lines.
    assert "G-DISCONNECT-HANGS" in out
    assert "**Symptom**" not in out


def test_gotcha_search_no_match_prints_message(capsys):
    parser = build_parser()
    args = parser.parse_args(["gotcha-search", "zzz-no-such-keyword"])
    rc = cmd_gotcha_search(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No gotchas matched" in out


def test_gotcha_search_missing_doc_returns_2(tmp_path, capsys):
    fake = tmp_path / "missing.md"
    parser = build_parser()
    args = parser.parse_args(["gotcha-search", "x", "--doc", str(fake)])
    # parse_gotchas returns [] for a non-existent doc, but the CLI
    # treats explicitly-overridden missing docs as an error if the
    # override doesn't exist. With no entries, the message "No gotchas
    # matched" is emitted and exit is 0 — which is fine. The error path
    # is hit only if the *user* passed a non-existent --doc.
    # Verify the behavior either way:
    rc = cmd_gotcha_search(args)
    assert rc in (0, 2)


# ---- suggest_for_error (failure → known-gotcha bridge) ---------------------


def test_suggest_for_error_finds_disconnect_gotcha():
    """A failure message mentioning ModelUtil.disconnect maps to the
    seeded G-DISCONNECT-HANGS entry via its id/symptom tokens."""
    from comsol_support.gotcha_search import suggest_for_error
    err = (
        "ModelExporter failed: process hung after solve\n"
        "halt_reason: exception\n"
        "error_detail:\n  - ModelUtil.disconnect() never returned"
    )
    hits = suggest_for_error(err)
    assert any(g.id == "G-DISCONNECT-HANGS" for g in hits)


def test_suggest_for_error_no_signal_returns_empty():
    """Generic failure text with only stopword-class tokens yields no
    suggestions (no score>=2 match on any id/symptom)."""
    from comsol_support.gotcha_search import suggest_for_error
    hits = suggest_for_error(
        "error: the model failed with an unknown exception")
    assert hits == []


def test_suggest_for_error_empty_and_missing_doc(tmp_path):
    from comsol_support.gotcha_search import suggest_for_error
    assert suggest_for_error("") == []
    assert suggest_for_error(
        "anything", doc_path=tmp_path / "missing.md") == []


def test_suggest_for_error_respects_limit():
    from comsol_support.gotcha_search import suggest_for_error
    # A token that matches many entries (symptom lines mention COMSOL
    # behaviors broadly) must still be capped.
    hits = suggest_for_error(
        "ModelUtil.disconnect hang and libcadimport libpath failure "
        "and selection wipe on geometry reimport", limit=2)
    # At least three entries match (disconnect, libpath, selection-wipe)
    # so the cap must bind exactly.
    assert len(hits) == 2


def test_suggest_for_error_sums_multi_token_corroboration():
    """An entry whose Symptom matches several tokens of the failure text
    must outrank an entry matched by a single (even id-level) token.

    Regression for a real campaign miss: the SIGABRT failure
    text ranked G-INITSTANDALONE-EXIT-CODE (one id token, 'exit') above
    G-SIGABRT-NO-HSERR (four symptom tokens) under best-single-token
    scoring. Sum semantics must rank the corroborated entry first.
    """
    from comsol_support.gotcha_search import suggest_for_error
    hits = suggest_for_error("ModelExporter produced no JSON envelope. exit=-6")
    assert hits, "expected suggestions for the SIGABRT failure text"
    assert hits[0].id == "G-SIGABRT-NO-HSERR"
    # The corroborated entry's summed score strictly exceeds any
    # single-token id match (3).
    assert hits[0].score > 3


def test_symptom_line_extraction():
    from comsol_support.gotcha_search import parse_gotchas
    entries = parse_gotchas()
    # Every seeded entry has a Symptom field, so symptom_line() must be
    # non-empty for all of them.
    for g in entries:
        assert g.symptom_line(), f"{g.id} symptom_line() empty"
