# Changelog

All notable changes to **minas-skill** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **POSIX backend (Linux / macOS).** `scripts/minas_smb.py` adds an
  `smbclient`-based backend: `SmbPath` (the subset of the `pathlib` API the CLI
  uses, every call shelling out to `smbclient`), plus `fs_copy2` /
  `fs_copytree` / `fs_move` / `fs_rmtree` / `fs_disk_usage` replacements so the
  command layer stays shared. Includes `smbclient -L` share listing, listing
  and free-space parsers, and exit-code mapping (2 = path missing, 3 =
  auth/network).
- `selftest` subcommand: read-only environment check (platform, backend, how
  credentials will be supplied, target share); `--raw` also dumps the
  underlying `smbclient` output, which is what you need when a server's listing
  format differs from the expected one.
- Offline tests for the backend: path handling, listing / free-space parsing,
  copy / move / rmtree, and password-file precedence.

### Fixed

- POSIX: the SMB password never reaches a process command line — credentials go
  into a `0600` auth file passed as `smbclient -A`, removed when the process
  exits.
- POSIX: `cp --force` onto an existing remote directory honours
  `dirs_exist_ok` (same semantics as the Windows `shutil` path).
- `ensure_session()` is a no-op on POSIX (each `smbclient` call authenticates on
  its own) instead of pretending to establish a session.

## [0.2.0] - 2026-10-26

### Fixed

- **Security**: SMB password is no longer passed on a process command line.
  Mapping now goes through Win32 `WNetAddConnection2W` (PowerShell
  `New-SmbMapping` fallback); only the interactive `*` prompt path remains
  for live password entry.
- Chinese Windows garbled `net` output / share names: decode with the ANSI
  code page (cp936) first instead of hard-coded UTF-8.
- `cp --force` on an existing directory tree no longer throws
  (`shutil.copytree(..., dirs_exist_ok=True)`).
- Password resolution priority is consistent with docs:
  `MINAS_PASSWORD_FILE` → interactive `*` → `MINAS_PASSWORD` / `MINAS_PASS`.
- `ensure_session` no longer tears down an existing mapping before trying to
  connect; `/delete` only runs on error 1219 (credential conflict).
- Chinese `net view` banner lines (`命令成功完成。` etc.) no longer leak into
  the share list.
- `locales/zh-CN.json` UTF-8 BOM removed.

### Added

- `cp --dry-run` (aligned with `rm` / `mv`).
- Regression tests for net decoding, password resolution, argv safety,
  `cp --force`, and Chinese `net view` noise.

## [0.1.0] - 2026-09-27

### Added

- Initial public release: UNC-constrained SMB CLI for Xiaomi Smart Storage
  (`shares` / `ls` / `du` / `tree` / `df` / `get` / `put` / `cat` / `mkdir` /
  `mv` / `cp` / `rm` / `find`), offline tests, gitleaks CI, Apache-2.0.

[Unreleased]: https://github.com/LUgABeEEE/minas-skill/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/LUgABeEEE/minas-skill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LUgABeEEE/minas-skill/releases/tag/v0.1.0
