#!/usr/bin/env bash
# Paruošia vieną CI mašiną valdikliui: rack-sleep, sudoers eilutė, ribotas Orange Pi
# raktas, Wake-on-LAN. Paleidžiama pačioje mašinoje:
#
#   sudo bash install.sh --user host3 --pubkey /tmp/rackctl.pub [--iface eno1]
#
# Kartojant nieko nedubliuoja. BIOS'e Wake-on-LAN įjungiamas ranka (docs/valdymas.md, 0 žingsnis).
set -euo pipefail

USER_NAME=""
PUBKEY=""
IFACE="eno1"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    --pubkey) PUBKEY="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    *) echo "nežinomas argumentas: $1" >&2; exit 2 ;;
  esac
done

die() { echo "klaida: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "paleisti su sudo"
[ -n "$USER_NAME" ] || die "nenurodytas --user"
id "$USER_NAME" >/dev/null 2>&1 || die "--user turi būti esama paskyra"
[ "$USER_NAME" != "ciuser" ] || die "ne ciuser: ta paskyra vykdo CI kodą"
[ -f "$PUBKEY" ] || die "--pubkey failas nerastas"
grep -qE '^ssh-(ed25519|rsa) ' "$PUBKEY" || die "--pubkey nėra viešasis SSH raktas"
[ -e "/sys/class/net/$IFACE" ] || die "sąsajos $IFACE nėra"
HERE="$(cd "$(dirname "$0")" && pwd)"

# 1. Užmigdymo komanda.
install -o root -g root -m 0755 "$HERE/rack-sleep" /usr/local/sbin/rack-sleep

# 2. sudo be slaptažodžio — tik šiai komandai. Tikrinama prieš padedant į vietą.
SUDOERS_TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/rack-sleep" > "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null || die "sudoers eilutė netinkama"
install -o root -g root -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/rack-sleep
rm -f "$SUDOERS_TMP"

# 3. Orange Pi raktas, ribotas viena komanda.
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$HOME_DIR/.ssh"
AUTH="$HOME_DIR/.ssh/authorized_keys"
touch "$AUTH"
chown "$USER_NAME:$USER_NAME" "$AUTH"
chmod 0600 "$AUTH"
KEY="$(awk '{print $1" "$2}' "$PUBKEY")"
if ! grep -qF "$KEY" "$AUTH"; then
  echo "command=\"sudo -n /usr/local/sbin/rack-sleep\",restrict $KEY rackctl" >> "$AUTH"
fi

# 4. Wake-on-LAN kiekvieno paleidimo metu (nustatymas po perkrovimo dingsta).
command -v ethtool >/dev/null || apt-get install -y -q ethtool
cat > /etc/systemd/system/wol@.service <<'UNIT'
[Unit]
Description=Wake-on-LAN magic packet on %i
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ethtool -s %i wol g

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "wol@$IFACE.service" >/dev/null
ethtool "$IFACE" | grep -q 'Wake-on: g' || die "Wake-on-LAN neįsijungė ($IFACE)"

echo "paruošta: $USER_NAME, $IFACE (Wake-on: g)"
