## What and why

## How it was checked

## Checklist

- [ ] Every commit is signed off (`git commit -s`)
- [ ] Tests cover the change; `python3 -m unittest discover -s tests -t .` passes
- [ ] `ruff check .`, `ruff format --check .` and `mypy` are clean
- [ ] No installation data (addresses, names, tokens); `python3 scripts/public_safe.py --history` passes
- [ ] `CHANGELOG.md` has a line under `## [Unreleased]` if users will notice the change
