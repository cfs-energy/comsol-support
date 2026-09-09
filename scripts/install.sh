#!/usr/bin/env bash
# install.sh — one-command bootstrap for comsol-support on Linux and macOS.
#
#   ./scripts/install.sh              full install (deps, Java, docs corpus)
#   ./scripts/install.sh --check      preflight only; change nothing
#   ./scripts/install.sh --no-corpus  skip the .mph corpus mining (the slow part)
#
# Every step is idempotent — re-running is safe and skips completed work.
# Windows users: run scripts/install.ps1 instead (same steps, same checks).
#
# The environment checks live in comsol_support/doctor.py, not here, so the
# three platforms cannot drift apart: this script decides *what to do*, and
# `doctor` decides *whether the machine can do it*.
#
# What it does NOT do: the Layer C slot harvest (`comsol-support scrape slots`)
# is left to you — it is a second full pass over the model library and holds a
# license seat throughout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
DO_CORPUS=1
for arg in "$@"; do
    case "$arg" in
        --check)     CHECK_ONLY=1 ;;
        --no-corpus) DO_CORPUS=0 ;;
        -h|--help)   sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }

echo "comsol-support install — $REPO_ROOT"
echo

# ---- uv -------------------------------------------------------------------
# The one true bootstrap prerequisite: uv provisions Python itself, so it is
# checked here rather than in doctor (which needs a Python to run at all).
if ! command -v uv >/dev/null 2>&1; then
    fail "uv not installed — https://docs.astral.sh/uv/"
    case "$(uname -s)" in
        Darwin) fail "  brew install uv" ;;
        *)      fail "  curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
    esac
    exit 1
fi
ok "uv: $(uv --version)"

# Corporate TLS-inspecting proxies re-sign PyPI; uv then rejects the chain
# with "invalid peer certificate: UnknownIssuer". Trusting the OS trust
# store fixes it and is a no-op elsewhere. (uv <0.9 spelled this
# UV_NATIVE_TLS; that name is deprecated and now warns, so it is not set.)
export UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}"

# ---- preflight ------------------------------------------------------------
# Prefer a system Python so --check truly changes nothing (no venv created).
# Fall back to uv's, which will provision one if the system has none.
PREFLIGHT=""
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
            >/dev/null 2>&1; then
        PREFLIGHT="$py"; break
    fi
done

echo
if [[ -n "$PREFLIGHT" ]]; then
    "$PREFLIGHT" scripts/doctor.py || exit 1
else
    warn "no system Python 3.10+; using uv's"
    uv run python scripts/doctor.py || exit 1
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    echo
    echo "Preflight passed. (--check: nothing was installed.)"
    exit 0
fi

# ---- install --------------------------------------------------------------
echo
echo "Installing Python package"
uv sync --extra dev
ok "uv sync complete"

echo
echo "Compiling COMSOL-dependent Java"
uv run python - <<'PY'
import comsol_support
from comsol_support.java_facade import JavaFacade, javac_diagnostics

jf = JavaFacade(comsol_path=comsol_support.COMSOL_PATH, workspace_dir="corpus")
result = jf.compile_comsol_sources()
if result.success:
    print(f"  compiled every source in {jf.java_source_dir}")
else:
    print("Compilation failed:")
    print(javac_diagnostics(result.stderr) or result.stderr[:800])
    raise SystemExit(1)
PY
ok "Java sources compile against the COMSOL classpath"

echo
echo "Building the knowledge base"
if [[ -s data/comsol.db ]]; then
    warn "data/comsol.db already exists — scrapers are additive, skipping"
    warn "delete it first if you want a clean rebuild"
else
    uv run comsol-support scrape javadoc
    if command -v pdftotext >/dev/null 2>&1; then
        uv run comsol-support scrape refmanual
        ok "Javadoc + Reference Manual ingested"
    else
        warn "pdftotext not found — skipping the Reference Manual"
        warn "install poppler and re-run: uv run comsol-support scrape refmanual"
    fi
fi

if [[ "$DO_CORPUS" -eq 1 ]]; then
    echo
    echo "Mining the .mph corpus (~10 minutes; holds a license seat)"
    # Bounded probe first. Without it, a busy license server turns the
    # last step of a first install into an indefinite hang with no
    # explanation — the worst possible first impression.
    if uv run comsol-support license-status --timeout 60 >/dev/null 2>&1; then
        uv run comsol-support scrape corpus
        ok "corpus mined"
    else
        warn "no COMSOL license seat free right now — skipping corpus mining"
        warn "everything else is installed; run this when a seat frees up:"
        warn "  uv run comsol-support scrape corpus"
    fi
else
    warn "skipped corpus mining (--no-corpus); 'fragments' table stays empty"
fi

echo
echo "Verifying"
uv run pytest -q 2>&1 | tail -3

echo
echo "Final check"
uv run comsol-support doctor || true

cat <<EOF

Done.

  Knowledge base : data/comsol.db
  Diagnostics    : uv run comsol-support doctor
  Next (optional): uv run comsol-support scrape slots -o data/slots_dump.jsonl
                   populates the Layer C catalog; a second full COMSOL pass.

EOF
