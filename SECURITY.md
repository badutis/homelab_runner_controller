# Security policy

WakeCI runs as a service on your network with GitHub tokens and an SSH key that can suspend
your machines, so security reports are welcome and handled first.

## Supported versions

Only the latest release gets fixes. WakeCI is before 1.0, so upgrade to the newest minor
version to receive a fix.

| Version | Supported |
|---|---|
| latest release | yes |
| older releases | no |

## Reporting a vulnerability

**Do not open a public issue.** Report it privately on GitHub:
**Security → Report a vulnerability**
(<https://github.com/badutis/wakeci/security/advisories/new>).

Please include:

- the version (`wakeci version`) and the host (OS, architecture);
- what an attacker needs (network position, an account, a token) and what they gain;
- steps to reproduce, ideally with a configuration that uses example values only.

What to expect:

- an acknowledgement within 7 days;
- an assessment and a plan within 30 days;
- a fix in a new release, credited to you unless you prefer otherwise, and a published
  security advisory.

## Scope

In scope:

- the controller (`wakeci/`) and its handling of tokens, the webhook URL and the SSH key;
- the installer (`controller/install.sh`, `controller/deploy.sh`) and the machine side
  (`machine/install.sh`, `machine/wakeci-sleep`): file modes and owners, the restricted SSH
  command, the `sudoers` rule;
- the release process: a tampered artifact that would pass `SHA256SUMS` or the provenance
  check.

Out of scope:

- GitHub itself, and your GitHub token permissions (WakeCI only needs read access);
- someone who already has root on the controller host or a runner machine;
- Wake-on-LAN being unauthenticated: any host on the same network segment can wake a
  machine. That is how Wake-on-LAN works; WakeCI does not add to it.

## How WakeCI is built to limit damage

The design and its threat model are in [`docs/design.md`](docs/design.md). In short: the
GitHub tokens are read-only, the SSH key can only run one command that suspends an idle
machine, secrets live in root-owned files that only the service user can read, and releases
carry checksums and a build provenance attestation.
