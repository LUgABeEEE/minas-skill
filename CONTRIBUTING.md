# Contributing to minas-skill

Thanks for helping improve the Xiaomi Smart Storage (MINAS) Samba agent skill.

## Ground rules

- **No secrets.** Never commit passwords, tokens, device IDs, cert material, or
  `minas_pw*.txt`. `MINAS_ROOT` sandboxing and UNC checks must stay strict.
- **Offline tests.** `tests/` must not touch a real NAS. Mock `net` / filesystem.
- **Windows-first.** Primary transport is SMB via UNC + `net use`. Document
  anything that only works on one Windows version or locale.
- **Agent-safe output.** Exit codes (`0/1/2/3`) and `--json` shapes are part of
  the contract — note breaking changes in `CHANGELOG.md`.

## Development setup

```powershell
git clone https://github.com/missfor100/minas-skill.git
cd minas-skill
python -m pytest tests/ -q
# or
python -m unittest discover -s tests -v
```

Python 3.10+ recommended. No third-party runtime dependencies.

## Pull requests

1. Fork and create a feature branch (`fix/…`, `feat/…`, `docs/…`).
2. Keep the diff focused; update `README.md` / `SKILL.md` / `docs/USAGE.md`
   together with behavior changes.
3. Add or extend tests for every bug fix.
4. Run the test suite and ensure it is green.
5. Fill in the PR template (what / why / how tested).

## Reporting bugs

Use the bug report template. Include: Windows version, Python version, the
exact command (redact host/user/password), expected vs actual, and whether
`net use` works in Explorer for the same share.

## Security issues

Do not open a public issue for credential leaks or path-escape bugs.
See [SECURITY.md](SECURITY.md).

## Code style

- Standard library only; type hints on public functions.
- Comments explain *why* a guard exists (especially around path safety).
- Prefer small pure helpers so they stay unit-testable offline.

## Release notes

Maintainers update `CHANGELOG.md` (Keep a Changelog) before tagging.
