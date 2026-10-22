#!/usr/bin/env bash
# Installs or updates the controller on the board it runs on. Run as root from an unpacked
# bundle (orangepi/deploy.sh builds and copies one):
#
#   sudo bash orangepi/install.sh [--config FILE] [--version VERSION]
#   sudo bash orangepi/install.sh --set-token NAME      # reads one token, nothing echoed
#
# The installed copy (/opt/rackctl/orangepi/install.sh) works the same way, so after the
# first install `sudo bash /opt/rackctl/orangepi/install.sh` re-runs it in place.
#
# Running it again only changes what differs. A run without --set-token never overwrites
# an existing SSH key, a known host key or a token. The service is (re)started only when the configuration
# and every token file it names are in place.
set -euo pipefail

ETC=/etc/rackctl
OPT=/opt/rackctl
UNIT=/etc/systemd/system/rackctl.service
JOURNALD_DROPIN=/etc/systemd/journald.conf.d/50-rackctl-volatile.conf
SVC_USER=rackctl

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "-- $*"; }

CONFIG=""
VERSION=""
TOKEN_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --set-token) TOKEN_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run with sudo"

# --- one token ---------------------------------------------------------------------------
if [ -n "$TOKEN_NAME" ]; then
  [[ "$TOKEN_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "token name must start with a letter or digit and hold only letters, digits, '.', '_' and '-'"
  id "$SVC_USER" >/dev/null 2>&1 || die "install the controller first"
  # Never one of the other files in $ETC.
  case "$TOKEN_NAME" in
    config.json|*.pub) die "$TOKEN_NAME is not a token file name" ;;
  esac
  if [ -f "$ETC/config.json" ] && PATHS="$(python3 "$OPT/orangepi/check_config.py" "$ETC/config.json")"; then
    if printf '%s\n' "$PATHS" | awk -F'\t' '$1 == "ssh_key" || $1 == "known_hosts" {print $2}' | grep -qxF "$ETC/$TOKEN_NAME"; then
      die "$ETC/$TOKEN_NAME is the SSH key or known_hosts, not a token"
    fi
  fi
  [ ! -L "$ETC/$TOKEN_NAME" ] || die "$ETC/$TOKEN_NAME is a symlink"

  if [ -t 0 ]; then
    printf 'token for %s/%s (not shown): ' "$ETC" "$TOKEN_NAME" >&2
    IFS= read -rs TOKEN; echo >&2
  else
    IFS= read -r TOKEN || true
  fi
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  [ -n "$TOKEN" ] || die "empty token, nothing written"
  TMP="$(mktemp "$ETC/.token.XXXXXX")"
  printf '%s\n' "$TOKEN" > "$TMP"
  unset TOKEN
  chown "$SVC_USER:$SVC_USER" "$TMP"
  chmod 0600 "$TMP"
  mv -f "$TMP" "$ETC/$TOKEN_NAME"
  note "written $ETC/$TOKEN_NAME (0600, $SVC_USER); run the installer again to start the service"
  exit 0
fi

[ -f "$SRC/rackctl/__main__.py" ] || die "no rackctl/ package next to orangepi/ (run from an unpacked bundle)"
for f in rackctl.service check_config.py config.example.json; do
  [ -f "$HERE/$f" ] || die "orangepi/$f missing"
done
[ -z "$VERSION" ] || [[ "$VERSION" =~ ^[A-Za-z0-9._+-]+$ ]] || die "bad --version"
[ -z "$CONFIG" ] || [ -f "$CONFIG" ] || die "--config file not found: $CONFIG"

# --- the board ---------------------------------------------------------------------------
MODEL="unknown"
[ -r /proc/device-tree/model ] && MODEL="$(tr -d '\0' < /proc/device-tree/model)"
MEM_MB="$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
note "board: $MODEL, $(uname -m), ${MEM_MB} MB RAM"

# --- packages ----------------------------------------------------------------------------
NEED=()
command -v python3 >/dev/null || NEED+=(python3)
for c in ssh ssh-keygen ssh-keyscan; do
  command -v "$c" >/dev/null || { NEED+=(openssh-client); break; }
done
# python3-minimal alone has the binary but not the whole standard library.
STDLIB='import compileall, http.server, ipaddress, json, urllib.request'
command -v python3 >/dev/null && ! python3 -c "$STDLIB" 2>/dev/null && NEED+=(python3)
if [ ${#NEED[@]} -gt 0 ]; then
  note "installing ${NEED[*]}"
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${NEED[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 is older than 3.9"
python3 -c "$STDLIB" || die "python3 lacks part of its standard library"

# --- the configuration, checked before anything changes ------------------------------------
if [ -n "$CONFIG" ]; then
  python3 "$HERE/check_config.py" "$CONFIG" >/dev/null || die "configuration rejected, nothing changed"
fi

# --- the account -------------------------------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$SVC_USER"
  note "created system user $SVC_USER"
fi

# --- the code ----------------------------------------------------------------------------
# Copied next to the old one and swapped in, so a failed copy leaves the old code working.
install -d -o root -g root -m 0755 "$OPT"
rm -rf "$OPT/rackctl.new" "$OPT/rackctl.old"
cp -R "$SRC/rackctl" "$OPT/rackctl.new"
find "$OPT/rackctl.new" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$OPT/rackctl.new"
find "$OPT/rackctl.new" -type d -exec chmod 0755 {} +
find "$OPT/rackctl.new" -type f -exec chmod 0644 {} +
# Compiled now, as root: the service may not write to /opt (ProtectSystem=strict).
python3 -m compileall -q "$OPT/rackctl.new" >/dev/null
[ -d "$OPT/rackctl" ] && mv "$OPT/rackctl" "$OPT/rackctl.old"
mv "$OPT/rackctl.new" "$OPT/rackctl"
rm -rf "$OPT/rackctl.old"
# The installer itself, so that tokens can be set and the install re-run later.
if [ "$SRC" != "$OPT" ]; then
  install -d -o root -g root -m 0755 "$OPT/orangepi"
  for f in install.sh check_config.py rackctl.service config.example.json; do
    install -o root -g root -m 0644 "$HERE/$f" "$OPT/orangepi/$f"
  done
fi
if [ -n "$VERSION" ]; then
  printf '%s\n' "$VERSION" > "$OPT/VERSION"
  chmod 0644 "$OPT/VERSION"
elif [ "$SRC" != "$OPT" ]; then
  rm -f "$OPT/VERSION"  # new code of an unknown version: the old number would be wrong
fi
note "code: $OPT/rackctl ($(cat "$OPT/VERSION" 2>/dev/null || echo "no version"))"

# --- the configuration -------------------------------------------------------------------
install -d -o root -g "$SVC_USER" -m 0750 "$ETC"
if [ -n "$CONFIG" ]; then
  install -o root -g "$SVC_USER" -m 0640 "$CONFIG" "$ETC/config.json"
  note "configuration: $ETC/config.json"
fi
if [ ! -f "$ETC/config.json" ]; then
  note "no $ETC/config.json yet: run again with --config FILE (start from orangepi/config.example.json)"
  START=no
else
  PATHS="$(python3 "$OPT/orangepi/check_config.py" "$ETC/config.json")" || die "$ETC/config.json is invalid"
  START=yes
fi

if [ "$START" = yes ]; then
  path_of() { printf '%s\n' "$PATHS" | awk -F'\t' -v k="$1" '$1 == k {print $2}'; }
  KEY="$(path_of ssh_key)"
  KNOWN="$(path_of known_hosts)"

  # check_config.py keeps both inside $ETC; a symlink there is refused, so the chown and
  # chmod below never follow one to another file.
  for f in "$KEY" "$KEY.pub" "$KNOWN"; do
    [ ! -L "$f" ] || die "$f is a symlink"
  done

  # --- SSH key for the machines (made here, never leaves the board) -----------------------
  if [ ! -f "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -C "rackctl@$(hostname)" -f "$KEY"
    note "new SSH key $KEY"
  fi
  chown -h "$SVC_USER:$SVC_USER" "$KEY"
  chmod 0600 "$KEY"
  [ -f "$KEY.pub" ] || ssh-keygen -y -f "$KEY" > "$KEY.pub"
  chmod 0644 "$KEY.pub"
  note "public key for machine/install.sh --pubkey:"
  cat "$KEY.pub"

  # --- host keys of the machines ------------------------------------------------------------
  # Trust on first use: a host is scanned only while it is not in the file. Compare the
  # printed fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the
  # machine. A sleeping machine cannot be scanned: run the installer again once it is up.
  touch "$KNOWN"
  chown -h root:"$SVC_USER" "$KNOWN"
  chmod 0644 "$KNOWN"
  while IFS= read -r host; do
    if ssh-keygen -F "$host" -f "$KNOWN" >/dev/null; then
      continue
    fi
    if SCAN="$(ssh-keyscan -T 5 -t ed25519 -- "$host" 2>/dev/null)" && [ -n "$SCAN" ]; then
      printf '%s\n' "$SCAN" >> "$KNOWN"
      note "host key of $host: $(printf '%s\n' "$SCAN" | ssh-keygen -lf - | awk '{print $2}')"
    else
      note "host $host did not answer (asleep?): its key is not known yet, it cannot be suspended"
    fi
  done < <(path_of host)

  # --- tokens ------------------------------------------------------------------------------
  while IFS= read -r token; do
    if [ -L "$token" ]; then
      note "token $token is a symlink: replace it with --set-token"
      START=no
    elif [ ! -f "$token" ]; then
      note "missing token $token: sudo bash $OPT/orangepi/install.sh --set-token $(basename "$token")"
      START=no
    elif ! runuser -u "$SVC_USER" -- test -r "$token"; then
      note "token $token is not readable by $SVC_USER"
      START=no
    fi
  done < <(path_of token)
  for f in "$KEY" "$KNOWN"; do
    runuser -u "$SVC_USER" -- test -r "$f" || { note "$f is not readable by $SVC_USER"; START=no; }
  done
fi

# --- less writing to the SD card ---------------------------------------------------------
install -d -m 0755 "$(dirname "$JOURNALD_DROPIN")"
WANT_JOURNALD="$(printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n')"
if [ "$(cat "$JOURNALD_DROPIN" 2>/dev/null)" != "$WANT_JOURNALD" ]; then
  printf '%s\n' "$WANT_JOURNALD" > "$JOURNALD_DROPIN"
  systemctl restart systemd-journald
  note "journal kept in RAM only ($JOURNALD_DROPIN)"
fi

# --- the service -------------------------------------------------------------------------
install -o root -g root -m 0644 "$HERE/rackctl.service" "$UNIT"
systemctl daemon-reload
systemctl enable rackctl.service >/dev/null || die "systemctl enable rackctl failed"
if [ "$START" != yes ]; then
  note "service enabled, NOT started: fix the lines above and run again"
  exit 0
fi
if ! systemctl restart rackctl.service; then
  journalctl -u rackctl.service -n 20 --no-pager >&2 || true
  die "systemctl restart rackctl failed"
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  systemctl is-active --quiet rackctl.service || break
done
systemctl is-active --quiet rackctl.service || {
  journalctl -u rackctl.service -n 20 --no-pager >&2 || true
  die "rackctl did not stay up"
}
note "rackctl is running: journalctl -u rackctl -f"
