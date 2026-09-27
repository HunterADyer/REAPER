#!/usr/bin/env bash
# =============================================================================
# setup_binja.sh — install/verify the Binary Ninja 6.0 headless drop-in.
#
# REAPER never pip-installs Binary Ninja (docs/binja-module.md §1, continue.md).
# Instead the headless release ships its own python/site-packages; this repo
# expects it on PYTHONPATH as $HOME/binja/python (or $HOME/binja-headless/python).
# `_compat.require_binja()` and the HLILExtractor/BNDBWriter guard every import,
# so the rest of the codebase stays importable even when Binja is absent.
#
# Usage:
#   bash scripts/setup_binja.sh [path/to/binja-headless-6.0-linux.tar.gz]
#
# If no tarball is given it scans ~/Downloads, ~/downloads, /tmp and $HOME for a
# likely Binary Ninja tarball first, then reports what is still missing.
# =============================================================================
set -u

TARGET="${1:-}"
BINJA_ROOT="$HOME/binja"
BINJA_PY="$BINJA_ROOT/python"
CANDIDATES=(
  "$HOME/Downloads" "$HOME/downloads" /tmp "$HOME"
)

# --- find the tarball if not given on argv --------------------------------
if [[ -z "$TARGET" ]]; then
  echo "setup_binja: no tarball given on argv; scanning for one..."
  for d in "${CANDIDATES[@]}"; do
    [[ -d "$d" ]] || continue
    hit=$(find "$d" -maxdepth 2 -type f \
          \( -iname '*binary*ninja*.tar*' -o -iname '*binja*.tar*' -o -iname '*binaryninja*.zip' \) 2>/dev/null | head -n1)
    if [[ -n "$hit" ]]; then
      TARGET="$hit"
      echo "setup_binja: found candidate: $hit"
      break
    fi
  done
fi

if [[ -z "$TARGET" || ! -f "$TARGET" ]]; then
  echo "setup_binja: no tarball found."
  echo "  Put the Binary Ninja 6.0 headless tarball anywhere, then rerun:"
  echo "    bash scripts/setup_binja.sh /path/to/binja-headless-6.0-linux.tar.gz"
  exit 1
fi

echo "==>> Extracting $TARGET -> $BINJA_ROOT"
mkdir -p "$BINJA_ROOT"
case "$TARGET" in
  *.zip)
    (command -v unzip >/dev/null 2>&1 && unzip -q -o "$TARGET" -d "$BINJA_ROOT") || \
      python3 -m zipfile -e "$TARGET" "$BINJA_ROOT" ;;
  *.tar.gz|*.tgz|*.tar)
    tar -xzf "$TARGET" -C "$BINJA_ROOT" ;;
  *) echo "setup_binja: unrecognized archive type: $TARGET (use .zip or .tar.gz)"; exit 2 ;;
esac
code=$?
if [[ $code -ne 0 ]]; then
  echo "setup_binja: extraction failed (exit $code)"
  exit $code
fi

# --- locate <...>/python/binaryninja however the tarball laid itself out ----
declare module_dir=""
while IFS= read -r d; do
  if [[ -f "$d/binaryninja/__init__.py" ]]; then
    module_dir="$(dirname "$d")"   # the python/ dir that must sit on PYTHONPATH
    break
  fi
done < <(find "$BINJA_ROOT" -maxdepth 4 -type d -name python 2>/dev/null)

if [[ -z "$module_dir" ]]; then
  echo "setup_binja: extracted, but no python/binaryninja package found under $BINJA_ROOT."
  echo "  (tarball layout unexpected — check: find $BINJA_ROOT -maxdepth 4 -name binaryninja)"
  exit 1
fi

# normalize: repo expects $HOME/binja/python/binaryninja
if [[ "$module_dir" != "$BINJA_PY" ]]; then
  rm -f "$BINJA_PY"
  ln -sfn "$module_dir" "$BINJA_PY"
fi

echo "==>> Binary Ninja python package: $BINJA_PY"
echo
echo "Use it with (add to ~/.bashrc):"
echo "  export PYTHONPATH=$BINJA_PY"
echo
echo "==>> License: ~/.binaryninja/license.dat"
if [[ -f "$HOME/.binaryninja/license.dat" ]]; then
  echo "  present ($(stat -c%s "$HOME/.binaryninja/license.dat" 2>/dev/null || echo unknown) bytes) — headless will auto-discover it"
else
  echo "  MISSING — copy a valid license to $HOME/.binaryninja/license.dat"
fi
echo
echo "==>> Smoke test:"
echo "  PYTHONPATH=$BINJA_PY python3 scripts/smoke_binja.py"
echo "  (opens eval/cjson/cjson_test with open_view, prints function/variable stats)"
