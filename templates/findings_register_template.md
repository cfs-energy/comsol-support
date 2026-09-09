# `<CAMPAIGN_ID>` — findings register

> One-line scope: <one sentence: what the campaign is benchmarking, building, or sweeping>
> Started: `YYYY-MM-DD` (cycle 1)
> Status: active | paused | concluded

The running ledger of every campaign-level finding. Entries follow the
`F-DOMAIN-PHENOMENON-α` convention defined in
[`finding_naming_convention.md`](finding_naming_convention.md).

## Schema

| Column | Meaning |
|---|---|
| **ID** | `F-DOMAIN-PHENOMENON-α/β/γ` (see naming-convention doc) |
| **Status** | `OPEN` → `SOURCE-1` → `SOURCE-2` → `CONFIRMED-Nx` → `CLOSED`/`SUPERSEDED` |
| **Opened** | Cycle # in which the finding was first recorded |
| **Last update** | Cycle # of last status change |
| **Description** | One-line. Keep ≤120 chars. Detail lives in the cycle log. |
| **Refs** | Cycle log file(s) + line numbers where the finding is discussed |

## Status lifecycle

```
       (first observed in a cycle)
OPEN ──────────────────► SOURCE-1
                              │
                  (replicated in another cycle / context)
                              ▼
                          SOURCE-2 ──────► CONFIRMED-2x
                                              │
                                   (replicated in a third)
                                              ▼
                                         CONFIRMED-3x ──► CLOSED
                                                          (or SUPERSEDED
                                                           if a later
                                                           finding subsumes
                                                           this one)
```

Rules:
- A finding never goes "backwards" — once CONFIRMED-2x, downgrade only
  with a clear refutation entry (and add a SUPERSEDED-BY column).
- Auditor-class findings start at SOURCE-1; researcher-class findings
  at OPEN.
- A finding cited as "the bug" in a fix that lands must be CLOSED in
  the same cycle as the fix.

## Findings

| ID | Status | Opened | Last update | Description | Refs |
|---|---|---|---|---|---|
| F-EXAMPLE-PROBE-α | OPEN | 1 | 1 | Example placeholder — replace before first real entry | cycle_logs/cycle_01.md |

<!-- New entries APPEND below this line, never in the middle. Status
     edits happen in place (no "history" column — the cycle logs are
     the history). -->
