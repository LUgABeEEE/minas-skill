# Changelog

All notable changes to **minas-skill** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/missfor100/minas-skill/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/missfor100/minas-skill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/missfor100/minas-skill/releases/tag/v0.1.0
