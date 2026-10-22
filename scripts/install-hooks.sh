#!/usr/bin/env bash
# Installs this repository's git hooks into the local clone (they are not versioned by git):
#
#   scripts/install-hooks.sh [--force]
#
# pre-push: runs scripts/public_safe.py on everything about to be pushed. The hook is
# copied, not linked, so a branch without scripts/pre-push still has it; on a branch whose
# guard is too old for it, it refuses the push and says so. Run the script again after
# scripts/pre-push changes.
#
# It refuses when core.hooksPath is set: that directory is shared (often by every repository
# of the user), and a relative one is resolved per worktree, so the hook would either break
# other repositories or miss some worktrees.
set -euo pipefail

FORCE=no
[ "${1:-}" = "--force" ] && FORCE=yes

root="$(git rev-parse --show-toplevel)"
cd "$root"
if hp="$(git config --get core.hooksPath)"; then
  echo "error: core.hooksPath is set ($hp); install scripts/pre-push there by hand if you want it" >&2
  exit 1
fi
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
