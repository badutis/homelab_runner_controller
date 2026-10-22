#!/usr/bin/env bash
# Installs one version of the controller on its host, from your own computer:
#
#   controller/deploy.sh [--ram-journal] <ssh-target> <git-ref> <config.json>
#   controller/deploy.sh --bundle-only <out.tar.gz> <git-ref> <config.json>
#
# --ram-journal is passed to install.sh: the host's journal is kept in RAM only. Use it on a
# board that runs from an SD card, not on a machine with a real disk.
#
# <git-ref> is normally a release tag (v0.1.0). The bundle holds that ref's code as git
# has it (`git archive`, not your working tree) and your config.json. It is copied with
# scp and installed with `sudo bash controller/install.sh`, so the host needs no GitHub
# access. sudo may ask for your password on the host.
#
# Tokens are never part of the bundle. Set them on the host afterwards:
#   ssh -t <ssh-target> sudo bash /opt/rackctl/controller/install.sh --set-token <name>
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() { sed -n '2,8p' "$0" >&2; exit 2; }
BUNDLE_ONLY=""
INSTALL_FLAGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle-only) BUNDLE_ONLY="${2:-}"; [ -n "$BUNDLE_ONLY" ] || die "--bundle-only needs a file name"; shift 2 ;;
    --ram-journal) INSTALL_FLAGS=" --ram-journal"; shift ;;
    -*) usage ;;
    *) break ;;
  esac
done
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
[ -z "$BUNDLE_ONLY" ] || [ -z "$INSTALL_FLAGS" ] \
  || die "--ram-journal is passed to the installer on the host; it has no effect with --bundle-only"

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
COMMIT="$(git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}")" || die "no such git ref: $REF"
VERSION="$(git -C "$REPO" describe --tags --exact-match "$COMMIT" 2>/dev/null || git -C "$REPO" rev-parse --short "$COMMIT")"
[[ "$VERSION" =~ ^[A-Za-z0-9._+-]+$ ]] || die "unexpected version string: $VERSION"
[ -f "$CONFIG" ] || die "config not found: $CONFIG"
# Versions before controller/ (v0.1.0) kept these scripts in orangepi/: deploy those with
# their own copy, `git show <ref>:orangepi/deploy.sh > /tmp/deploy-old.sh`.
git -C "$REPO" cat-file -e "$COMMIT:controller/install.sh" 2>/dev/null \
  || die "$VERSION has no controller/install.sh (an older layout); deploy it with that version's own deploy.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/bundle"
git -C "$REPO" archive --format=tar "$COMMIT" rackctl controller LICENSE | tar -x -C "$WORK/bundle"
cp "$CONFIG" "$WORK/bundle/config.json"
# The same check the host runs, with the code of that version, before anything is copied.
python3 "$WORK/bundle/controller/check_config.py" "$WORK/bundle/config.json" >/dev/null \
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
# VERSION and NAME are checked above to hold no shell characters; INSTALL_FLAGS is a constant.
# shellcheck disable=SC2029
ssh -t "$TARGET" "set -e; d=\$(mktemp -d); trap 'rm -rf \"\$d\" $REMOTE' EXIT; \
  tar -xzf $REMOTE -C \"\$d\"; \
  sudo bash \"\$d/controller/install.sh\" --config \"\$d/config.json\" --version $VERSION$INSTALL_FLAGS"
