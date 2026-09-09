#!/usr/bin/env bash
# Installed-wheel smoke test.
#
# comsol-support is a source-checkout project: a wheel built from
# pyproject.toml deliberately omits the Java sources, the probe library
# and docs/known-gotchas.md. This script proves that such an install
# fails EXPLICITLY (doctor: blocking "Source checkout" problem;
# gotcha-search: clear error pointing at INSTALL.md) rather than with a
# bare javac usage error. Runs in CI (.github/workflows/ci.yml) and
# locally: `bash scripts/wheel_smoke.sh`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT"
uv build --wheel --out-dir "$WORK/dist" -q
WHEEL="$(ls "$WORK"/dist/*.whl)"
echo "built $WHEEL"

if unzip -l "$WHEEL" | grep -q '\.java$'; then
    echo "unexpected: wheel carries .java sources — update this smoke and INSTALL.md" >&2
    exit 1
fi

uv venv -q "$WORK/venv"
VIRTUAL_ENV="$WORK/venv" uv pip install -q "$WHEEL"
AGENT="$WORK/venv/bin/comsol-support"
cd "$WORK"   # away from the checkout: nothing may resolve via cwd

echo "--- doctor"
set +e
DOCTOR_OUT="$("$AGENT" doctor 2>&1)"; DOCTOR_RC=$?
set -e
echo "$DOCTOR_OUT" | grep -i "source checkout" || { echo "doctor did not report the source checkout" >&2; echo "$DOCTOR_OUT"; exit 1; }
[ "$DOCTOR_RC" -ne 0 ] || { echo "doctor exited 0 from a wheel install" >&2; exit 1; }
echo "$DOCTOR_OUT" | grep -q "INSTALL.md" || { echo "doctor fix text lacks the INSTALL.md pointer" >&2; exit 1; }

echo "--- gotcha-search"
set +e
GS_OUT="$("$AGENT" gotcha-search disconnect 2>&1)"; GS_RC=$?
set -e
[ "$GS_RC" -eq 2 ] || { echo "gotcha-search exit $GS_RC, expected 2" >&2; echo "$GS_OUT"; exit 1; }
echo "$GS_OUT" | grep -q "INSTALL.md" || { echo "gotcha-search error lacks the INSTALL.md pointer" >&2; echo "$GS_OUT"; exit 1; }

echo "--- license-status (facade guard)"
set +e
LS_OUT="$("$AGENT" license-status --timeout 5 2>&1)"; LS_RC=$?
set -e
[ "$LS_RC" -ne 0 ] || { echo "license-status exited 0 from a wheel install" >&2; exit 1; }
echo "$LS_OUT" | grep -q "source checkout" || { echo "license-status did not name the source checkout:" >&2; echo "$LS_OUT"; exit 1; }

echo "--- --help still works"
"$AGENT" --help >/dev/null

echo "wheel smoke OK: installed wheel fails explicitly, as documented"
