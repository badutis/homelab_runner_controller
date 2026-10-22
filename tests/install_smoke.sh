#!/usr/bin/env bash
# Installs the controller from scratch on a disposable Linux machine with systemd and
# checks the result. CI runs it on a GitHub-hosted runner; it needs passwordless sudo and
# changes the system (user, /opt, /etc, a systemd unit), so do not run it on a real host.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
mode_owner() { sudo stat -c '%a %U' "$1"; }

W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT

step "bundle"
controller/deploy.sh --bundle-only "$W/b.tar.gz" HEAD controller/config.example.json
mkdir "$W/x"
tar -xzf "$W/b.tar.gz" -C "$W/x"

step "first install: no tokens yet, so the service must not start"
OUT="$(sudo bash "$W/x/controller/install.sh" --config "$W/x/config.json" --version smoke 2>&1)" \
  || { echo "$OUT"; fail "first install failed"; }
echo "$OUT"
grep -q "NOT started" <<<"$OUT" || fail "service started without tokens"
grep -q "missing token /etc/rackctl/token-example-org:" <<<"$OUT" || fail "missing token not reported"
id rackctl >/dev/null || fail "no rackctl user"
[ "$(getent passwd rackctl | cut -d: -f7)" = /usr/sbin/nologin ] || fail "rackctl can log in"
[ "$(mode_owner /etc/rackctl)" = "750 root" ] || fail "/etc/rackctl: $(mode_owner /etc/rackctl)"
[ "$(mode_owner /etc/rackctl/id_ed25519)" = "600 rackctl" ] || fail "key: $(mode_owner /etc/rackctl/id_ed25519)"
[ "$(mode_owner /etc/rackctl/config.json)" = "640 root" ] || fail "config: $(mode_owner /etc/rackctl/config.json)"
[ -f /opt/rackctl/rackctl/__main__.py ] || fail "package not at /opt/rackctl/rackctl"
[ -f /opt/rackctl/controller/install.sh ] || fail "installer not kept in /opt/rackctl/controller"
[ "$(cat /opt/rackctl/VERSION)" = smoke ] || fail "VERSION"
[ ! -e /etc/systemd/journald.conf.d/50-rackctl-volatile.conf ] || fail "journal changed without --ram-journal"
systemctl is-enabled --quiet rackctl || fail "service not enabled"
! systemctl is-active --quiet rackctl || fail "service active without tokens"
FP1="$(sudo ssh-keygen -lf /etc/rackctl/id_ed25519)"  # the private key itself

step "tokens"
if printf '\n' | sudo bash /opt/rackctl/controller/install.sh --set-token token-empty; then
  fail "empty token accepted"
fi
for bad in ../escape .. config.json id_ed25519 known_hosts id_ed25519.pub; do
  if printf 'x\n' | sudo bash /opt/rackctl/controller/install.sh --set-token "$bad"; then
    fail "token name $bad accepted"
  fi
done
[ "$(mode_owner /etc/rackctl/config.json)" = "640 root" ] || fail "config.json touched by --set-token"
printf 'not-a-real-token\n' | sudo bash /opt/rackctl/controller/install.sh --set-token token-example-org
printf '  not-a-real-token-2 \n' | sudo bash /opt/rackctl/controller/install.sh --set-token token-example-org2
[ "$(mode_owner /etc/rackctl/token-example-org)" = "600 rackctl" ] || fail "token mode"
[ "$(sudo cat /etc/rackctl/token-example-org2)" = not-a-real-token-2 ] || fail "token not trimmed"

step "second install, in place, with --ram-journal: starts, keeps the key"
OUT="$(sudo bash /opt/rackctl/controller/install.sh --ram-journal 2>&1)" || { echo "$OUT"; fail "second install failed"; }
echo "$OUT"
grep -q "rackctl is running" <<<"$OUT" || fail "service did not start"
[ "$(sudo ssh-keygen -lf /etc/rackctl/id_ed25519)" = "$FP1" ] || fail "SSH key was replaced"
[ "$(cat /opt/rackctl/VERSION)" = smoke ] || fail "VERSION lost on an in-place run"
grep -qx 'Storage=volatile' /etc/systemd/journald.conf.d/50-rackctl-volatile.conf || fail "--ram-journal had no effect"
grep -q "host: .*, $(uname -m), " <<<"$OUT" || fail "host line missing"

step "metrics"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:9105/metrics 2>/dev/null | grep -q '^rackctl_dry_run 1$'; then
    curl -fsS http://127.0.0.1:9105/metrics
    echo; echo "OK"
    exit 0
  fi
  sleep 1
done
sudo journalctl -u rackctl -n 30 --no-pager || true
fail "no /metrics with rackctl_dry_run 1"
