#!/usr/bin/env bash
# Rehearse the complete offline release gate for one exact committed revision.
set -Eeuo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <40-character-commit-sha>" >&2
    exit 2
fi

REVISION=$1
if [[ ! "$REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Release revision must be an exact 40-character lowercase commit SHA." >&2
    exit 2
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

resolved_revision=$(git rev-parse --verify "${REVISION}^{commit}")
head_revision=$(git rev-parse --verify HEAD)
if [ "$resolved_revision" != "$REVISION" ] || [ "$head_revision" != "$REVISION" ]; then
    echo "Release gate must run from the detached worktree for $REVISION." >&2
    exit 2
fi

require_clean_revision() {
    current_head=$(git rev-parse --verify HEAD)
    if [ "$current_head" != "$REVISION" ] \
        || [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
        echo "Release gate requires an unchanged clean exact-revision worktree." >&2
        exit 2
    fi
}

require_clean_revision

PYTHON=${PYTHON_BIN:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1 && [ ! -x "$PYTHON" ]; then
    echo "PYTHON_BIN does not identify an executable Python interpreter: $PYTHON" >&2
    exit 2
fi
for tool in ruff mypy plutil; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required release tool is unavailable: $tool" >&2
        exit 2
    fi
done

TEMP_ROOT=""
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$TEMP_ROOT" ] && [ -d "$TEMP_ROOT" ]; then
        rm -rf -- "$TEMP_ROOT"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/nrt-release-gate.XXXXXX")
SITE_DIR="$TEMP_ROOT/site"
MYPY_CACHE_DIR="$TEMP_ROOT/mypy-cache"

"$PYTHON" -m unittest discover -s . -p 'test_*.py' -v
"$PYTHON" validate_pipeline.py \
    --articles articles_index.json \
    --trades trades_extracted.json \
    --manifest snapshot_manifest.json
ruff check ./*.py
mypy --cache-dir "$MYPY_CACHE_DIR"
"$PYTHON" -m py_compile ./*.py

for file in ./*.sh ./.githooks/pre-push; do
    if [ -f "$file" ]; then
        bash -n "$file"
    fi
done
plutil -lint launchd/com.navnoor.substacktrades.plist

# A regression or quality tool must never be able to mutate the inputs that
# are about to receive the already-selected revision identity.
require_clean_revision
test ! -e "$SITE_DIR"
SITE_OUTPUT_DIR="$SITE_DIR" SITE_REVISION="$REVISION" \
    "$PYTHON" build_site.py
"$PYTHON" validate_inline_scripts.py "$SITE_DIR/index.html"
"$PYTHON" validate_release.py \
    --site "$SITE_DIR" \
    --articles articles_index.json \
    --trades trades_extracted.json \
    --manifest snapshot_manifest.json \
    --expected-revision "$REVISION"

git diff --check
require_clean_revision
echo "Release gate passed for $REVISION"
