#!/usr/bin/env bash
# Installs this repository's git hooks into the local clone (they are not versioned by git):
#
#   scripts/install-hooks.sh [--force]
#
# pre-push: runs scripts/public_safe.py on everything about to be pushed. The hook is
# copied, not linked, so it keeps working on a branch that does not have this file. Run
# the script again after scripts/pre-push changes.
set -euo pipefail

FORCE=no
[ "${1:-}" = "--force" ] && FORCE=yes

root="$(git rev-parse --show-toplevel)"
cd "$root"
hooks="$(git rev-parse --git-path hooks)"
src="$root/scripts/pre-push"
dst="$hooks/pre-push"
mkdir -p "$hooks"
if [ -e "$dst" ] && ! cmp -s "$src" "$dst" && [ "$FORCE" != yes ]; then
  echo "error: $dst exists and differs; run with --force to replace it" >&2
  exit 1
fi
cp "$src" "$dst"
chmod 0755 "$dst"
echo "installed $dst"
