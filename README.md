# homelab_runner_controller

Wakes self-hosted GitHub Actions runner machines when jobs for them are queued, and
suspends them after they have been idle for a while. It runs on a small always-on board
(written for an Orange Pi Zero with 512 MB RAM) on the same network segment as the
machines.

- **Wake:** a Wake-on-LAN magic packet, sent when queued jobs outnumber the free online
  runners that could take them.
- **Sleep:** one restricted SSH command (`rack-sleep`) that suspends the machine (S3)
  only if no job is running on it.
- **Read-only towards GitHub:** it polls the REST API with read-only tokens. It needs no
  inbound connection, webhook or token that can change anything.
- **Python standard library only** (3.9+), no Docker, no database. The only state on disk
  is a small JSON file, written a few times a day.
- **`dry_run` by default:** it only logs what it would do until you turn that off.

How the decisions are made, the security model and the known limits are described in
[`docs/design.md`](docs/design.md).

## Layout

| Path | Runs on | What it does |
|---|---|---|
| `rackctl/decide.py` | controller | when to wake and when to suspend; pure logic, no I/O |
| `rackctl/github.py` | controller | reads runners and queued jobs (GET only, ETag caching) |
| `rackctl/actions.py` | controller | Wake-on-LAN packet, suspend over SSH |
| `rackctl/main.py` | controller | the loop, `/metrics`, the state file |
| `orangepi/install.sh` | controller | installs or updates the controller on the board |
| `orangepi/deploy.sh` | your computer | packs one version with your configuration and installs it on the board |
| `orangepi/check_config.py` | both | checks a configuration before it is installed |
| `orangepi/` (other files) | controller | example configuration and the `systemd` unit |
| `machine/rack-sleep` | runner machine | suspends the machine unless a job is running |
| `machine/install.sh` | runner machine | installs `rack-sleep`, the `sudoers` rule, the restricted key and Wake-on-LAN |
| `scripts/public_safe.py` | CI | refuses private addresses, MAC addresses, personal emails and AI attribution |

## Requirements

- Runner machines with Wake-on-LAN and working suspend-to-RAM, enabled in the BIOS.
  Check this by hand on one machine first: suspend it, wake it with a packet from
  another machine, and confirm its runners come back online in GitHub.
- The controller on the **same layer-2 segment** (VLAN) as the machines. Wake-on-LAN
  broadcasts do not cross routers.
- Runners registered with a name prefix per machine (for example `host1-a-1`,
  `host1-a-2`), so that the controller can tell which machine a runner belongs to.

## Install

**On each runner machine** (as root, from a checkout of this repository):

```bash
sudo bash machine/install.sh --user rackctl --ci-user runner --pubkey /tmp/rackctl.pub
```

- `--user` is the account the controller logs in as. It gets the controller's public key,
  restricted to the single command `sudo -n /usr/local/sbin/rack-sleep`.
- `--ci-user` is the account that runs CI jobs. The script refuses to give it the key.
- `--iface` (default `eno1`) is the interface Wake-on-LAN is enabled on.

Running the script again changes nothing.

**On the controller board.** Start from a minimal Debian-based image. For an Orange Pi
Zero that is Armbian's *minimal* image (Debian 13): no desktop, no Docker. SSH into it
as a user with `sudo`. Then, from a checkout of this repository on your own computer:

```bash
orangepi/deploy.sh <ssh-target> v0.1.0 /path/to/your/config.json
```

`deploy.sh` packs that tag's code (`git archive`, not your working tree) and your
`config.json`, checks the configuration with that version's checker (the SSH key,
`known_hosts` and the token files must be directly in `/etc/rackctl/`), copies the bundle
with `scp` and runs `orangepi/install.sh` on the board with `sudo`. The board needs no
access to GitHub for this.

`install.sh` runs as root on the board. It:

1. prints the board model, architecture and memory; installs `python3` and
   `openssh-client` if missing (Python 3.9 or newer is required);
2. creates the system user `rackctl` (no home, no login shell);
3. puts the code at `/opt/rackctl/rackctl/` (owned by root, precompiled) and a copy of
   the installer at `/opt/rackctl/orangepi/`;
4. installs the configuration as `/etc/rackctl/config.json` (`0640`, group `rackctl`);
5. creates the SSH key for the machines **on the board**, if there is none yet
   (`/etc/rackctl/id_ed25519`, `0600`, owner `rackctl`), and prints its public key for
   `machine/install.sh --pubkey`;
6. scans the host key of every machine that is not in `known_hosts` yet, and prints its
   fingerprint. Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the
   machine. A sleeping machine cannot be scanned: run the installer again once it is
   awake. Until then the controller cannot suspend it;
7. keeps the systemd journal in RAM (`Storage=volatile`), to spare the SD card;
8. installs and enables `rackctl.service`, and starts it only if every token file the
   configuration names exists and is readable by `rackctl`.

A run without `--set-token` never replaces an existing SSH key, host key or token. Run it
again at any time:

```bash
sudo bash /opt/rackctl/orangepi/install.sh
```

**Tokens** are typed on the board, never put in the bundle:

```bash
ssh -t <ssh-target> sudo bash /opt/rackctl/orangepi/install.sh --set-token token-example-org
```

This reads the token without echoing it and writes `/etc/rackctl/<name>` (`0600`, owner
`rackctl`). The name must match the `token_file` in your configuration. When every
token is in place, run the installer again to start the service.

**Watch it** with `journalctl -u rackctl -f` and `curl http://<board>:9105/metrics`.
While `"dry_run": true`, it only logs what it would do. When the log looks right, set
`"dry_run": false` in your configuration and deploy again.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | log decisions without sending packets or SSH commands |
| `poll_seconds` | `20` | how often GitHub is polled |
| `idle_minutes` | `30` | how long all runners of a machine must be idle before it is suspended |
| `broadcast` | `255.255.255.255` | where Wake-on-LAN packets are sent (the segment's broadcast address) |
| `metrics_listen` | `0.0.0.0:9105` | address of the `/metrics` endpoint |
| `state_file` | `/var/lib/rackctl/last-online.json` | when each machine was last online |
| `ssh_key` | — | private key used for `rack-sleep` |
| `known_hosts` | `/etc/rackctl/known_hosts` | host keys of the machines (checked strictly) |
| `sources[]` | — | where runners are registered and whose jobs use them (below) |
| `machines[]` | — | the machines (below) |

A **source** is one place where runners are registered, plus the repositories whose jobs
those runners take:

- `runners_path`: `repos/<owner>/<repo>` for repository runners, or `orgs/<org>` for
  organisation runners;
- `repos`: the repositories whose queued jobs are watched;
- `token_file`: a file that holds a read-only token for this source.

A runner only counts for a job from the same source, and only if it has every label
the job asks for. This is the same rule GitHub uses to assign jobs.

A **machine** has:

- `name`;
- `mac` and `host`;
- `ssh_user` (defaults to `name`);
- `runner_prefixes`: every runner whose name starts with one of them belongs to this
  machine;
- `rank`: lower ranks are woken first, so give faster machines a lower number.

### Tokens

Use one fine-grained personal access token per source, with read access only:

- **repository source:** Actions: read, and Administration: read (needed to list
  repository runners);
- **organisation source:** Actions: read on the repositories, and Self-hosted runners:
  read on the organisation.

Check the exact permission names when you create the tokens: GitHub renames them from
time to time. A stolen token can read the queue and nothing else.

## Metrics

`GET /metrics` returns the Prometheus text format:

| Metric | Type | Labels |
|---|---|---|
| `rackctl_machine_awake` | gauge | `machine` |
| `rackctl_machine_waking` | gauge | `machine` |
| `rackctl_actions_total` | counter | `kind`, `machine` |
| `rackctl_runner_busy_seconds_total` | counter | `runner` |
| `rackctl_queued_jobs` | gauge | `labels` |
| `rackctl_queued_oldest_seconds` | gauge | `labels` |
| `rackctl_github_ok` | gauge | `source` |
| `rackctl_github_remaining` | gauge | `source` |
| `rackctl_dry_run` | gauge | — |

The names are pinned by a test (`tests/test_io.py`). A monitoring system that scrapes
them breaks without any error when a name changes.

## Development

```bash
python3 -m unittest discover -s tests -t .
shellcheck machine/install.sh machine/rack-sleep orangepi/install.sh orangepi/deploy.sh tests/install_smoke.sh
python3 scripts/public_safe.py --history
```

CI runs these on GitHub-hosted runners, and also `tests/install_smoke.sh`: it installs
the controller from scratch on a clean Ubuntu with systemd and starts it with fake tokens.
That script changes the system, so never run it on a real board. It never runs on the self-hosted runners,
because those are the machines this controller suspends.

**Nothing installation-specific goes into this repository.** That means no real IP or
MAC addresses, no machine, user or repository names, no tokens and no personal email
addresses. Examples use the documentation ranges (`192.0.2.0/24`,
`00:00:5e:00:53:xx`) and `example-org`. Your real configuration lives in your own
infrastructure repository.

`scripts/public_safe.py` catches the leaks that have a recognisable shape: private
addresses, MAC addresses, personal emails and AI attribution. It checks the whole history
of the branch and every tag's message and tagger, not only the latest commit. It cannot recognise public
addresses or the names of real machines, users and repositories. Keeping those out is
up to the author.

## Status

Not yet deployed. Log messages and code comments are still in Lithuanian. They will be
translated in a separate change.
