#!/usr/bin/env bash
# One-shot installer for the "invest" DSH skill (macOS / Linux).
#
# Usage (agent-friendly, no repo checkout needed):
#   curl -fsSL https://raw.githubusercontent.com/blz89/invest-skill/main/install.sh | bash
#
# What it does:
#   1. clone or update https://github.com/blz89/invest-skill into the DSH skills dir
#   2. create the bundled Python venv (stock/.venv)
#   3. install requirements.txt, the bundled stock/ package (editable) and pypdf
#   4. run the offline self-test suite (scripts/selftest_all.py --fast)
#
# Overridable via environment variables:
#   INVEST_SKILL_DIR        install directory (default: ~/.dsh/skills/invest)
#   INVEST_SKILL_REF        branch or tag (default: main)
#   INVEST_SKILL_SKIP_TEST  set to 1 to skip the self-test
#
# Exit code: 0 = installed and verified, non-zero = failed (safe to fail closed).

set -euo pipefail

DIR="${INVEST_SKILL_DIR:-$HOME/.dsh/skills/invest}"
REPO="${INVEST_SKILL_REPO:-https://github.com/blz89/invest-skill.git}"
REF="${INVEST_SKILL_REF:-main}"
SKIP_TEST="${INVEST_SKILL_SKIP_TEST:-0}"

log() { printf '==> %s\n' "$*"; }
ok()  { printf 'OK  %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git not found in PATH"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  die "Python 3.10+ not found in PATH"
fi
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ required"

mkdir -p "$(dirname "$DIR")"

if [ -d "$DIR/.git" ]; then
  log "Updating existing checkout at $DIR"
  git -C "$DIR" fetch --depth 1 origin "$REF" || die "git fetch failed (ref: $REF)"
  git -C "$DIR" checkout -q FETCH_HEAD || die "git checkout failed"
else
  if [ -e "$DIR" ]; then
    [ -z "$(ls -A "$DIR" 2>/dev/null || true)" ] || die "$DIR exists and is not empty"
    rmdir "$DIR"
  fi
  log "Cloning $REPO ($REF) into $DIR"
  git clone --depth 1 --branch "$REF" "$REPO" "$DIR" || die "git clone failed"
fi

VENV="$DIR/stock/.venv"
VPY="$VENV/bin/python"
if [ ! -x "$VPY" ]; then
  log "Creating Python venv: stock/.venv"
  "$PY" -m venv "$VENV" || die "failed to create venv (is the venv module available?)"
fi

log "Installing Python dependencies"
"$VPY" -m pip install --upgrade pip --quiet || die "pip upgrade failed"
"$VPY" -m pip install -r "$DIR/requirements.txt" --quiet || die "pip install -r requirements.txt failed"
"$VPY" -m pip install -e "$DIR/stock" --quiet || die "pip install -e stock failed"
"$VPY" -m pip install pypdf --quiet || die "pip install pypdf failed"

if [ "$SKIP_TEST" = "1" ]; then
  log "Skipping offline self-test (INVEST_SKILL_SKIP_TEST=1)"
else
  log "Running offline self-test (python scripts/selftest_all.py --fast)"
  ( cd "$DIR" && A_SHARE_OFFLINE=1 PYTHONIOENCODING=utf-8 TQDM_DISABLE=1 "$VPY" scripts/selftest_all.py --fast ) \
    || die "offline self-test failed"
fi

printf '\n'
ok "installed: $DIR"
ok "python:    $VPY"
printf 'DSH will pick it up as skill "invest" on the next session.\n'
printf 'Try: "%s" "%s" --capital 50000\n' "$VPY" "$DIR/scripts/run.py"
