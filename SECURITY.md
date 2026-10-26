# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | yes |
| < 0.2 | best-effort only |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for:

- credential leakage or password handling flaws
- path traversal / `MINAS_ROOT` sandbox escapes
- command injection in `net` invocation
- anything that would expose another user's NAS share

Instead, use GitHub **Private vulnerability reporting** on this repository
(Security → Report a vulnerability), or contact the maintainer privately.

Include a minimal reproduction (redact hostnames, usernames, passwords) and
the impact you observed. You should receive an acknowledgement within a few
days.

## Non-vulnerability reports

For ordinary bugs (wrong exit code, docs, locale quirks), a public issue is
fine.

## Hard rules in this project

- Passwords must never appear on a process command line or in logs.
- Prefer `MINAS_PASSWORD_FILE` (ACL-restricted) or the interactive prompt.
- Remote operations must stay behind UNC validation and `MINAS_ROOT`.
- No secrets in source, tests, fixtures, or screenshots.
