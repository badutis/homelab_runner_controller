#!/usr/bin/env bash
# Installs one version of the controller on the board, from your own computer:
#
#   orangepi/deploy.sh <ssh-target> <git-ref> <config.json>
#   orangepi/deploy.sh --bundle-only <out.tar.gz> <git-ref> <config.json>
#
# <git-ref> is normally a release tag (v0.1.0). The bundle holds that ref's code as git
# has it (`git archive`, not your working tree) and your config.json. It is copied with
# scp and installed with `sudo bash orangepi/install.sh`, so the board needs no GitHub
# access. sudo may ask for your password on the board.
#
# Tokens are never part of the bundle. Set them on the board afterwards:
#   ssh -t <ssh-target> sudo bash /opt/rackctl/orangepi/install.sh --set-token <name>
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

BUNDLE_ONLY=""
if [ "${1:-}" = "--bundle-only" ]; then
  BUNDLE_ONLY="${2:-}"
  shift 2 || die "--bundle-only needs a file name"
fi
usage() { sed -n '2,6p' "$0" >&2; exit 2; }
if [ -n "$BUNDLE_ONLY" ]; then
  [ $# -eq 2 ] || usage
  TARGET=""
else
  [ $# -eq 3 ] || usage
  TARGET="$1"
  shift
fi
REF="$1"
CONFIG="$2"

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
COMMIT="$(git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}")" || die "no such git ref: $REF"
VERSION="$(git -C "$REPO" describe --tags --exact-match "$COMMIT" 2>/dev/null || git -C "$REPO" rev-parse --short "$COMMIT")"
[[ "$VERSION" =~ ^[A-Za-z0-9._+-]+$ ]] || die "unexpected version string: $VERSION"
[ -f "$CONFIG" ] || die "config not found: $CONFIG"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/bundle"
git -C "$REPO" archive --format=tar "$COMMIT" rackctl orangepi LICENSE | tar -x -C "$WORK/bundle"
cp "$CONFIG" "$WORK/bundle/config.json"
# The same check the board runs, with the code of that version, before anything is copied.
python3 "$WORK/bundle/orangepi/check_config.py" "$WORK/bundle/config.json" >/dev/null \
  || die "config rejected by $VERSION"
NAME="rackctl-$VERSION"
# No macOS resource forks or extended attributes in the bundle (GNU and BSD tar both
# know --no-xattrs).
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$WORK/$NAME.tar.gz" -C "$WORK/bundle" .

if [ -n "$BUNDLE_ONLY" ]; then
  cp "$WORK/$NAME.tar.gz" "$BUNDLE_ONLY"
  echo "bundle $VERSION: $BUNDLE_ONLY"
  exit 0
fi

echo "deploying $VERSION ($COMMIT) to $TARGET"
REMOTE="/tmp/$NAME.tar.gz"
scp -q "$WORK/$NAME.tar.gz" "$TARGET:$REMOTE"
# VERSION and NAME are checked above to hold no shell characters.
# shellcheck disable=SC2029
ssh -t "$TARGET" "set -e; d=\$(mktemp -d); trap 'rm -rf \"\$d\" $REMOTE' EXIT; \
  tar -xzf $REMOTE -C \"\$d\"; \
  sudo bash \"\$d/orangepi/install.sh\" --config \"\$d/config.json\" --version $VERSION"
