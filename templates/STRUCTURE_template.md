# `<CAMPAIGN_ID>` — workspace structure

Standard layout for a long-running COMSOL research campaign workspace.
Copy this file into your campaign's root as `STRUCTURE.md`.

```
<CAMPAIGN_ID>/
├── STRUCTURE.md                          # this file (from comsol-support/templates)
├── <CAMPAIGN_ID>_plan_of_record.md       # human-edited campaign plan
├── <CAMPAIGN_ID>_findings_register.md    # see findings_register_template.md
├── <CAMPAIGN_ID>_campaign_state.json     # see campaign_state_template.json
│
├── _plan/                                # human-defined milestones
│   └── milestones.json
├── _run/                                 # orchestrator-class events
│   ├── cycle_001.md
│   ├── cycle_002.md
│   └── …
├── _archive/                             # superseded artifacts (one subdir per cycle)
└── _orphan/                              # files the orchestrator could not classify
│
├── builders/                             # *.java builders (mphgen contract)
├── mutators/                             # *.java mutators (edit-mph contract)
├── reference/                            # .mph files the campaign is benchmarked against
├── output/                               # generated .mph + sidecars (gitignored)
├── plots/                                # post-processed figures
├── analysis/                             # analysis scripts, notebooks (gitignored if large)
└── notes/                                # human-edited free-form notes
```

## Reserved namespaces

| Directory | Owner | Append rules |
|---|---|---|
| `_plan/` | Human (campaign lead) | Edit in place; never auto-mutated |
| `_run/` | Orchestrator | One file per cycle; append-only; never edit a closed cycle |
| `_archive/` | Orchestrator | Add on supersession; never delete |
| `_orphan/` | Orchestrator | Move (don't delete) files the classifier doesn't recognize |

This separation is what makes `<CAMPAIGN_ID>_campaign_state.json`
authoritative: every file has exactly one owner.

## Tools that read this structure

- `comsol-support search` — indexes `_run/*.md` + `output/*.telemetry.jsonl`
- `comsol-support gotcha-search` — separate from campaign state; just
  greps `comsol-support/docs/known-gotchas.md`
- Any campaign-internal `promise_check.py` extension that uses
  `<CAMPAIGN_ID>_campaign_state.json` as the source of truth

## When to deviate

The `_plan/`, `_run/`, `_archive/`, `_orphan/` quartet is load-bearing
across campaigns and should NOT be renamed or split — many internal
tools key on these names.

Everything else (`builders/`, `mutators/`, `reference/`, etc.) is
suggestive. Adopt the names that match your campaign's primary noun
(`stacks/`, `magnets/`, `cables/`, …) if those are clearer.
