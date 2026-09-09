"""One-line rendering of ``knowledge`` rows, shared by the CLI ``search``
verb and the MCP ``search_api`` tool so both surfaces answer identically."""

from __future__ import annotations


def format_knowledge_row(r: dict) -> str:
    """Render one knowledge row as ``Class.method(sig) [prop: k] | stage= module=``
    followed by an indented description line."""
    sig = r.get("signature") or ""
    prop = r.get("property_key") or ""
    desc = r.get("description") or ""
    head = r.get("class") or ""
    if r.get("method"):
        head += "." + r["method"]
    if sig:
        head += f"({sig})"
    if prop:
        head += f" [prop: {prop}]"
    return (f"{head} | stage={r.get('stage') or ''}"
            f" module={r.get('module') or ''}\n  {desc}")
