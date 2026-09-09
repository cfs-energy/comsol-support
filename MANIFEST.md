# MANIFEST — comsol-support

A map of the repository: what each module does, what the database
holds, and how the pieces feed each other. `README.md` explains why
the project is shaped this way; this file is the index.

## comsol_support/ — core modules

`comsol_agent/` remains as a two-file compatibility shim: `__init__.py`
registers every `comsol_support.<module>` under `comsol_agent.<module>`
(the same module objects), and `mcp_server.py` delegates to
`comsol_support.mcp_server` for pre-1.0 MCP configurations.

| Module | Purpose |
|---|---|
| `cli.py` | CLI entry point and the 13 subcommands. Subparsers are registered by the modules that own them via `add_*_subparser` |
| `config.py` | Path-only configuration dataclass. `db_path` resolves `$COMSOL_DB` first, then `<checkout>/data/comsol.db` |
| `_source_checkout.py` | Source-checkout detection. A wheel install omits the Java sources, the probes and `docs/known-gotchas.md`; `doctor` reports that as blocking, `JavaFacade` raises up front, and `cli.main` turns it into one `error:` line |
| `doctor.py` | Cross-platform install preflight: COMSOL discovery, platform libraries, bundled JDK, plugin JARs, doc trees, optional tools, knowledge-base contents. Read-only, ASCII output, never raises |
| `db.py` | The single SQLite store: 7 user tables, 2 FTS5 virtual tables, 6 sync triggers, 14 indexes. Migrations are additive `ALTER TABLE ADD COLUMN` in try/except |
| `knowledge_format.py` | One `format_knowledge_row` shared by CLI `search` and MCP `search_api` so both render identically |
| `java_facade.py` | Java compile/run wrapper. `find_comsol_jars()` returns every plugin JAR (an OSGi requirement); sets the platform library path; forces `-Djava.awt.headless=true`. Java discovery order: `PATH` → COMSOL's bundled JRE → `JAVA_HOME`. `run_class` runs any COMSOL-dependent class in its own process group with a tee'd log and group-kill on timeout, and decodes abnormal exits to signal names plus any `hs_err_pid*.log` |
| `mcp_server.py` | MCP stdio server exposing 10 tools. JSON-RPC 2.0, Content-Length framed. `COMSOL_DB` gates the database path. No auth. A crashing tool handler returns JSON-RPC -32603 rather than killing the loop |
| `javadoc_scraper.py` | Javadoc ontology scraper: HTML → `ClassRecord` → knowledge table. Idempotent per source |
| `refmanual_scraper.py` | Reference Manual scraper: `pdftotext` plus property-table parsing → knowledge table. Owns the shared stage classifier |
| `corpus_miner.py` | Corpus mining: environment verify, batch `.mph → .java` conversion, parsing, fragment ingestion with co-occurrence scoring |
| `linting.py` | Three-layer lint orchestration and the `check` subcommand. Layer A is the A1–A6 regex catalog plus a description scan; Layer B bridges to `ModelChecker.java`; Layer C bridges to `dimensional.py`. Emits a merged sidecar. Classifies `.mph` completeness and skips `.partial.mph` unless `--allow-partial` |
| `dimensional.py` | Layer C symbolic dimensional analysis, pure Python. Expression tokenizer/parser → AST; dimensions as `Fraction`-exponent vectors over the SI base dimensions; explicit unit table with prefix decomposition and per-unit scale factors; scope-aware symbol resolution with multi-pass feedback |
| `slot_catalog.py` | Prescriptive slot catalog: five-axis subvariant key, confidence tiering, batched aggregation, and the expected-unit overrides Layer C consumes |
| `slot_harvest.py` | Harvest driver — one JVM subprocess wrapping `SlotHarvester.java` over a batch of `.mph`, streaming crash-safe JSONL |
| `native_catalog.py` | Native-interface catalog: the retrieval side of the native-first ladders in `docs/modeling-practice.md` |
| `telemetry.py` | Solver and run telemetry: parses `TELEMETRY:` lines, writes crash-safe per-event JSONL, builds a digest (event counts, halt reason, error detail, saved paths, results), tees the raw JVM stream, and ingests sidecars into SQLite |
| `mphgen.py` | Builder `.java → .mph`. Compile, run `ModelExporter`, optionally solve, capture the telemetry sidecar, validate the produced `.mph`, then run the post-build lint |
| `edit_mph.py` | Load an existing `.mph`, optionally mutate, optionally solve, save. In-place edits are atomic — the JVM writes a temp file that is validated and then replaced over the input; the temp is discarded on every failure path |
| `run_harness.py` | Thin CLI over `JavaFacade.run_class` for arbitrary COMSOL-dependent classes. Warns loudly when the output log ends up empty |
| `query_mph.py` | Read-only sibling of `edit-mph`: loads, runs a `query(Model, Map)` contract, prints JSON, never saves. Bare probe names resolve against the shipped probe library |
| `license_status.py` | Bounded license-seat probe over `LicenseProbe.java`, with no `lmutil` dependency |
| `jvm_slot.py` | Advisory exclusive-JVM slot (per-user lock, warn by default, `COMSOL_AGENT_EXCLUSIVE=1` to enforce) plus a leaked-JVM scan on all three platforms |
| `gotcha_search.py` | Symptom-indexed retrieval over `docs/known-gotchas.md`, ranked by id / symptom / body match. Also powers the failure hints printed by `mphgen` and `edit-mph` |

## comsol_support/java/ — the typed facade and harnesses

| File | Purpose |
|---|---|
| `TagRegistry.java` | Unique type-prefixed tags with serialize/deserialize. Compiles without a COMSOL classpath |
| `SelectionAlgebra.java` | Selection primitives and boolean operations with serialize/deserialize. Compiles without a COMSOL classpath |
| `ModelExporter.java` | Dual-mode build/edit entry point. Build reflects `static Model buildModel(Map<String,String>)`; edit loads an `.mph` and reflects `static Model mutate(Model, Map<String,String>)`. Both share optional solve, the `TELEMETRY:` stream, a solver heartbeat, tee'd solver output, partial-save on every failure path, post-solve probe extraction, and a mesh verb |
| `ModelChecker.java` | Layer B runtime linter. Walks parameters via `ParamBase.evaluateUnit` and component variables via `ExpressionBase`, writing unit / description / symbol sidecars |
| `SlotHarvester.java` | Walks components → physics interfaces → features → properties, filtering real expressions from booleans and blanks, and emits JSONL slot records |
| `SolverTelemetry.java` | Solver event emitter for stationary, time-dependent and parametric studies. `emitError` harvests a full exception chain in one call. No COMSOL classpath needed to compile |
| `SolverHeartbeat.java` | Standalone "still alive" emitter for long solves — one daemon thread on a fixed cadence, callback exceptions swallowed, idempotent stop. No COMSOL classpath needed |
| `CorpusBatchConverter.java` | Batch `.mph → .java` converter: single JVM, per-file error handling, JSON progress, resume support |
| `LicenseProbe.java` | Bounded seat probe — checks out a license and reports timing, or is classified unavailable by the caller's timeout |
| `NodeTreeProbe.java` | Debug utility that walks a loaded model and emits a condensed tree summary |
| `probes/` | Read-only query-contract probe library (mesh statistics and unmeshed census, per-feature build records that survive an aggregate throw, feature and child selections). Resolved by bare name via `query-mph --query <Name>` |

## CLI surface

`comsol-support <subcommand>` — 13 subcommands, pinned by
`tests/test_cli.py`. The `comsol-agent` console script is an alias for
the same entry point.

| Subcommand | Module | Role |
|---|---|---|
| `search` | `cli.py` | Knowledge-base lookup; same backend as MCP `search_api` |
| `scrape {javadoc,refmanual,corpus,slots}` | scrapers | Ingest into the knowledge, fragment and slot tables |
| `slot-stats`, `promote-catalog` | `slot_catalog.py`, `native_catalog.py` | Slot statistics; corpus-frequency promotion |
| `mphgen` | `mphgen.py` | Builder → `.mph` |
| `edit-mph` | `edit_mph.py` | Existing `.mph` → mutate → save |
| `query-mph` | `query_mph.py` | Existing `.mph` → read-only query → JSON, never saves |
| `run-harness` | `run_harness.py` | Compile and run any COMSOL-dependent Java class |
| `license-status` | `license_status.py` | Bounded license-seat probe |
| `check` | `linting.py` | Three-layer lint on a `.java` or an `.mph` |
| `gotcha-search` | `gotcha_search.py` | Search the gotcha catalog |
| `ingest-telemetry` | `telemetry.py` | Backfill telemetry sidecars into the database |
| `doctor` | `doctor.py` | Cross-platform install preflight |

## Database schema

One SQLite file: 7 user tables plus 2 FTS5 virtual tables.

| # | Table | Role | Notable columns |
|---|---|---|---|
| 1 | `knowledge` | Javadoc + Reference Manual ontology | class, method, signature, property_key, value_type, stage, module, source, description |
| 2 | `fragments` | Corpus-mined Java code blocks | stage, tier, pattern_name, java_code, corpus_freq, co_occurrence_json, source |
| 3 | `telemetry_events` | Solver and run events (`build_id` is a caller-chosen run key) | build_id, output_path, event_type, wall_ms, payload_json, created_at |
| 4 | `slot_expected_units` | Layer C prescriptive catalog | six-tuple composite key + n_attempts, n_resolved, coverage_frac, modal_unit, modal_count, modal_frac, distribution, confidence, type_source |
| 5 | `variable_declared_units` | Symbol-resolver seed | six-tuple composite key + n_occurrences |
| 6 | `native_interfaces` | Native-preference catalog | three-tuple composite key + class_name, setup_cost_rank, corpus_freq, default_studies, default_plots, auto_features, notes |
| 7 | `domain_synonyms` | Keyword → domain map | intent_keyword, domain_keyword |
| FTS | `knowledge_fts` | FTS5 over the knowledge table | class, method, signature, property_key, description, stage, module |
| FTS | `telemetry_fts` | FTS5 over telemetry payloads | event_type, payload_json, build_id, output_path |

The database is always built locally from a licensed COMSOL
installation and is never distributed — see `docs/data-provenance.md`.

## MCP server — 10 tools

JSON-RPC 2.0 over stdio, Content-Length framed. Server name
`comsol-support-tools`. `COMSOL_DB` gates the database path. No auth.

| Tool | Purpose |
|---|---|
| `get_fragment` | Fragment by id |
| `search_api` | Knowledge search, optional stage scope (CLI twin: `comsol-support search`) |
| `search_fragments` | Fragments by stage and optional pattern |
| `get_solver_telemetry` | Telemetry events, with a `since_id` tail for polling |
| `get_solver_results` | Result probes and globals |
| `search_telemetry` | Full-text search over telemetry payloads |
| `list_physics_options` | Native interfaces for a domain |
| `list_studies_for_physics` | Default studies for a physics tag |
| `list_default_plots_for_physics` | Default plots and probes for a physics tag |
| `search_gotchas` | Gotcha catalog by symptom keyword |

## docs/

| File | Purpose |
|---|---|
| `mphgen.md` | Builder → `.mph`: contract, CLI, exit codes, retrofit recipes |
| `mphedit.md` | Existing `.mph` → mutate → save: mutator contract, canonical patterns, telemetry envelope, exit codes |
| `query-mph.md` | The read-only query contract, its serializer, CLI and Python API |
| `run-harness.md` | The generic harness runner, thread dumps, and bounding a license checkout |
| `solver-progress.md` | Why COMSOL 6.4 exposes no per-step solver callback, and the heartbeat pattern that substitutes |
| `known-gotchas.md` | The `G-DOMAIN-PHENOMENON` catalog, queryable via `gotcha-search` |
| `meshing.md` | Mesh doctrine for large assemblies: commit semantics, census-not-throw scoring, sequence authoring, swept sourcing, quality measures, probe-before-mutate |
| `linting.md` | The three-layer architecture, the A1–A6 catalog, the sidecar schema, Layer C detail |
| `modeling-practice.md` | Stage-by-stage modeling practice: working discipline, the native-first ladders, per-stage invariants and pitfalls with API quick-reference |
| `facade-conventions.md` | `TagRegistry` and `SelectionAlgebra`: tag discipline, prefix table, selection discipline, API tables |
| `platform-setup.md` | Linux / macOS / Windows setup: prerequisites, auto-discovery, knowledge-base extraction, verification |
| `macos-setup.md` | macOS specifics on Apple Silicon |
| `data-provenance.md` | Artifact-by-artifact provenance, the ship-scrapers-never-data rule, and the CI guard |
| `comsol_linting_research.md` | Design research behind the linter |

## templates/

Format-only starting points for organizing long-running modeling work
(a findings register, a state file, a naming convention, a directory
layout). No project-specific content; copy and rename.

## Data flow

| Origin | Consumer | Data |
|---|---|---|
| `db.search_knowledge` + `knowledge_format.format_knowledge_row` | `cli.cmd_search`, MCP `search_api` | Identical rendering on both surfaces |
| `native_catalog.lookup_*` | MCP physics / studies / plots tools | Native-first ladder retrieval |
| `javadoc_scraper.scrape_javadoc` | `db.store_knowledge_batch` | Class / method / signature rows |
| `refmanual_scraper.scrape_reference_manual` | `db.store_knowledge_batch` | Property keys and descriptions |
| `refmanual_scraper.classify_feature_stage` | `corpus_miner.ingest_fragments` | Stage assignment for fragments |
| `corpus_miner.mine_corpus` | `cli._scrape_corpus` | Full mining pipeline |
| `java_facade.JavaFacade` | `mphgen`, `edit_mph`, Layer B, `corpus_miner`, scrapers | Compile and run with the full classpath and library path |
| `linting.cmd_check` → `dimensional.run_dimensional_check` | `.dimensional.json` sidecar | Layer C output |
| `slot_harvest.run_harvest` → `SlotHarvester.java` | JSONL dump | Raw slot records |
| `slot_catalog.aggregate_dump` | slot tables | Aggregated catalog with confidence tiers |
| `slot_catalog.build_expected_overrides_from_slots` | `dimensional.analyze_model` | Expected-unit overrides |
| `telemetry.stream_telemetry_lines` | `mphgen.run_mphgen` | Live event capture during a solve |
| `telemetry.ingest_sidecar_to_db` | `ingest-telemetry`, `mphgen --ingest-db` | JSONL → `telemetry_events` |
| `mphgen.run_mphgen` | post-build lint | Layer A on the `.java`, Layers B and C on the `.mph` |

## Layer C contract — where expected units come from

```
SlotHarvester.java  ──JSONL──►  slot_harvest.run_harvest
                                       │
                                       ▼
                            slot_catalog.aggregate_dump
                       (batched Layer C analysis; outcome
                        classification; modal aggregation;
                        confidence tiering)
                                       │
                                       ▼
                            slot_expected_units table
                                       │
              ┌────────────────────────┴────────────────────────┐
              ▼                                                 ▼
   build_expected_overrides                          slot_catalog.lookup_
   _from_slots                                       expected_unit
   (Layer C contract input)                          (CLI `slot-stats` query)
              │
              ▼
   dimensional.analyze_model  ──►  W2/W3 mismatch findings
   (slot expected unit  vs.
    Layer C deduced unit,
    dimension equality — pure Python)
```

## Known scope limits

- **Layer B is narrower than the layer diagram suggests.**
  `ModelChecker` evaluates units on global parameters; the COMSOL Java
  API exposes no per-variable equivalent, and the PDE-slot warnings
  visible in the GUI are unreachable from Java. Layer C plus the slot
  catalog is the substitute.
- **Batch `.mph → .java` conversion may need a model server at scale.**
  The single-JVM converter is sufficient for the bundled corpus.

Further deferred work is tracked in [`gaps.md`](gaps.md); linter
false positives with fix sketches are in `LINT_BACKLOG.md`.
