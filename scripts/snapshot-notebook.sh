#!/usr/bin/env bash
#
# Run a notebook headless and produce timestamped snapshots:
#
#   reports/notebooks/<category>/<name>_<UTC_TIMESTAMP>.ipynb   (executed)
#   reports/html/<category>/<name>_<UTC_TIMESTAMP>.html         (rendered)
#
# Where <category> is the parent directory name of the input notebook
# (so e.g. notebooks/backtest/ema_cross.ipynb → category=backtest).
#
# Why this exists: when you click "Run All" inside Jupyter / VS Code /
# Cursor, the in-notebook "save" cell runs against the *stale* on-disk
# file because cell outputs haven't been autosaved yet — producing an
# empty/incomplete snapshot.  This script avoids that race entirely:
# nbconvert manages the kernel itself and writes the executed copy
# atomically.
#
# Usage:
#   ./scripts/snapshot-notebook.sh notebooks/backtest/ema_cross.ipynb
#
# Or with a custom output basename:
#   ./scripts/snapshot-notebook.sh notebooks/backtest/ema_cross.ipynb my_run
#
# Requires: jupyter (already in the project venv).
set -euo pipefail

if [[ $# -lt 1 ]]; then
    cat <<USAGE
Usage: $0 <path/to/notebook.ipynb> [output_basename]

Executes the notebook headless and writes timestamped snapshots:
  reports/notebooks/<category>/<basename>_<UTC_TIMESTAMP>.ipynb
  reports/html/<category>/<basename>_<UTC_TIMESTAMP>.html

If output_basename is omitted, derives from the input filename.
USAGE
    exit 1
fi

INPUT="$1"
if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: file not found: $INPUT" >&2
    exit 1
fi

NB_DIR="$(dirname "$INPUT")"
NB_FILE="$(basename "$INPUT" .ipynb)"
CATEGORY="$(basename "$NB_DIR")"
TS="$(date -u +%Y%m%d_%H%M%S)"

# Project root = parent of scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NB_OUT_DIR="$PROJECT_ROOT/reports/notebooks/$CATEGORY"
HTML_OUT_DIR="$PROJECT_ROOT/reports/html/$CATEGORY"
mkdir -p "$NB_OUT_DIR" "$HTML_OUT_DIR"

BASENAME="${2:-$NB_FILE}_${TS}"

# Locate jupyter.  Walk upwards from the script directory looking for
# a .venv/ — handles both standalone projects and git worktrees that
# share the parent project's venv.  Falls back to PATH.
JUPYTER=""
search_dir="$PROJECT_ROOT"
for _ in 1 2 3 4 5; do
    for candidate in \
        "$search_dir/.venv/Scripts/jupyter.exe" \
        "$search_dir/.venv/bin/jupyter"; do
        if [[ -x "$candidate" ]]; then
            JUPYTER="$candidate"
            break 2
        fi
    done
    parent="$(dirname "$search_dir")"
    [[ "$parent" == "$search_dir" ]] && break  # reached fs root
    search_dir="$parent"
done
if [[ -z "$JUPYTER" ]]; then
    JUPYTER="$(command -v jupyter 2>/dev/null || true)"
fi
if [[ -z "$JUPYTER" ]]; then
    echo "ERROR: jupyter not found." >&2
    echo "Searched up to 5 parent dirs from $PROJECT_ROOT for .venv/, also tried PATH." >&2
    echo "Activate the project venv first, or install jupyter in your environment." >&2
    exit 1
fi
echo "Using jupyter: $JUPYTER"
echo

echo "Executing notebook (this may take a few minutes)..."
echo "  Input : $INPUT"
echo "  Output: $NB_OUT_DIR/$BASENAME.ipynb"
echo

"$JUPYTER" nbconvert \
    --execute \
    --to notebook \
    --output-dir "$NB_OUT_DIR" \
    --output "$BASENAME" \
    "$INPUT"

echo
echo "Rendering HTML..."
"$JUPYTER" nbconvert \
    --to html \
    --output-dir "$HTML_OUT_DIR" \
    --output "$BASENAME" \
    "$NB_OUT_DIR/$BASENAME.ipynb"

echo
echo "✓ Snapshot saved:"
echo "  notebook: $NB_OUT_DIR/$BASENAME.ipynb"
echo "  html    : $HTML_OUT_DIR/$BASENAME.html"
