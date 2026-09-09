# Security policy

## What this project does with your system

comsol-support runs **local** subprocesses only: `javac` / `java` from
the COMSOL-bundled JDK (or your PATH), the COMSOL runtime itself,
`pdftotext`, and `uv`. It opens no network connections of its own —
the only network activity is COMSOL's license checkout, which is
COMSOL's, not ours. It reads the COMSOL install read-only and writes
under the repository (`data/`, `corpus/`, telemetry sidecars next to
your `.mph` files) and directories you name on the command line.

Things worth knowing before pointing it at untrusted inputs:

- **`mphgen`, `edit-mph`, `query-mph` and `run-harness` compile and
  execute Java you give them** with the full COMSOL classpath and your
  user's privileges. Treat a builder / mutator / probe / harness `.java`
  file like any other program you run.
- The MCP server (`comsol-support`'s `mcp_server.py`) speaks JSON-RPC
  over **stdio only**, has no authentication, and is meant to be
  launched as a subprocess by a local agent (e.g. Claude Code
  `--mcp-config`). Do not expose it on a socket.
- The knowledge base is a local SQLite file; `COMSOL_DB` / `--db`
  point at it. Queries use parameterised SQL and FTS5 `MATCH` — an FTS
  syntax error is reported, not executed.

## Supported versions

Only the latest release on `main` receives fixes. COMSOL Multiphysics
**6.4** is the only supported COMSOL version (`COMSOL_VERSIONS.md`).

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Use
GitHub's private vulnerability reporting for this repository
(*Security → Report a vulnerability*) if it is enabled, or contact a
repository maintainer privately. Include the COMSOL build
(`comsol-support doctor` output), the command, and a minimal
reproduction. You should hear back within a week; fixes ship as a
normal release with a CHANGELOG entry crediting the reporter unless
you prefer otherwise.
