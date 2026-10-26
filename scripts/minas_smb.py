#!/usr/bin/env python3
# Copyright 2026 missfor100
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""POSIX backend for minas-skill: talk SMB through the `smbclient` binary.

Windows drives UNC paths through the OS (``\\\\host\\share\\...`` behaves like a
local path). POSIX has no such thing, so this module provides:

* :class:`SmbPath` — a small ``pathlib.Path`` look-alike implementing only what
  ``minas.py`` needs (``stat``/``iterdir``/``is_dir``/``is_file``/``mkdir``/
  ``unlink``/``read_bytes`` …), each call shelling out to ``smbclient``.
* ``fs_*`` helpers — drop-in replacements for the ``shutil`` calls that used to
  receive a UNC path as a string (``copy2``/``copytree``/``move``/``rmtree``/
  ``disk_usage``). They fall back to the real ``shutil`` when no ``SmbPath`` is
  involved, so the same call sites work on both platforms.
* :func:`list_shares` — share enumeration via ``smbclient -L``.

Design notes
------------
* **No password ever reaches argv.** Credentials go into a ``0600`` temporary
  file passed with ``-A``; when no username is configured we use ``-N``.
* **Listings are cached per (host, share, dir)** for the lifetime of the
  process, because ``ls`` asks ``stat()`` on every child and each ``smbclient``
  invocation is a full round trip.
* **Parsing fails loudly.** If ``smbclient`` output cannot be parsed we raise
  with the offending line instead of silently returning wrong metadata. Run
  ``minas.py selftest <path>`` to validate the parser against your own server.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path
from typing import Any, Iterable

SMBCLIENT = shutil.which("smbclient")

# Same shape as shutil.disk_usage's return value, without touching the private
# shutil._ntuple_diskusage.
_DiskUsage = namedtuple("usage", "total used free")

# smbclient's `dir`/`ls` line:
#   name.txt                            A     1234  Sat Oct 11 15:00:00 2026
_LS_ENTRY = re.compile(
    r"^(?P<name>.+?)\s{2,}(?P<attr>[A-Z]{1,8})\s+(?P<size>\d+)"
    r"\s+(?P<date>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} [\d:]+\s+\d{4})$"
)
# trailing " 12345 blocks of size 1024. 6789 blocks available" (dir/du)
_LS_FOOTER = re.compile(
    r"(\d+)\s+blocks of size\s+(\d+)\.?\s+(\d+)\s+blocks available", re.I
)
# Errors that mean "network/auth problem" → exit 3, vs "does not exist" → exit 2.
_NET_ERRORS = (
    "NT_STATUS_LOGON_FAILURE",
    "NT_STATUS_ACCESS_DENIED",
    "NT_STATUS_ACCOUNT_DISABLED",
    "NT_STATUS_CONNECTION_REFUSED",
    "NT_STATUS_CONNECTION_RESET",
    "NT_STATUS_HOST_UNREACHABLE",
    "NT_STATUS_NETWORK_UNREACHABLE",
    "NT_STATUS_IO_TIMEOUT",
    "session setup failed",
    "Connection to",
)
_NOTFOUND_ERRORS = (
    "NT_STATUS_BAD_NETWORK_NAME",
    "NT_STATUS_OBJECT_NAME_NOT_FOUND",
    "NT_STATUS_OBJECT_PATH_NOT_FOUND",
    "NT_STATUS_NO_SUCH_FILE",
)

_ENTRY_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
_AUTH_FILE: str | None = None


class SmbError(OSError):
    """SMB operation failed. ``exit_code`` follows the CLI convention (2/3)."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def classify(text: str) -> int:
    """Map smbclient error text onto the CLI exit-code convention."""
    for token in _NET_ERRORS:
        if token in text:
            return 3
    for token in _NOTFOUND_ERRORS:
        if token in text:
            return 2
    return 1


def _require_smbclient() -> str:
    if not SMBCLIENT:
        raise SmbError(
            "smbclient not found; install Samba's client (Debian/Ubuntu: "
            "`apt install smbclient`) or run this tool on Windows",
            exit_code=1,
        )
    return SMBCLIENT


# ---------------------------------------------------------------- credentials

def default_cred_file() -> Path:
    """Shared credentials file — same location minas-webdav uses."""
    explicit = os.environ.get("MINAS_CRED_FILE", "").strip()
    if explicit:
        return Path(explicit)
    home = os.environ.get("DSH_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".dsh"
    return base / "minasCred" / "credentials.env"


_CRED_ENV: dict[str, str] | None = None
_HOST_CRED_NOTE_SHOWN = False


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _cred_file_env() -> dict[str, str]:
    """KEY=VALUE pairs from the shared credentials file (cached, best effort)."""
    global _CRED_ENV
    if _CRED_ENV is None:
        _CRED_ENV = {}
        for line in _read_lines(default_cred_file()):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            _CRED_ENV[key.strip()] = value.strip().strip('"').strip("'")
    return _CRED_ENV


def setting(name: str, default: str = "") -> str:
    """Resolve a MINAS_* setting: environment → shared credentials file."""
    value = os.environ.get(name)
    if value:
        return value
    return _cred_file_env().get(name, default)


def _fstab_credentials(host: str, share: str) -> tuple[str, str]:
    """Linux last resort: reuse the credentials the host already mounts this share with.

    Looks up ``/etc/fstab`` for a cifs entry whose source is this share (or a
    subdirectory of it) and reads the file its ``credentials=`` option points
    at. This is what lets SMB access to an already-mounted NAS share work with
    no configuration at all.

    When the share itself is not mounted (``IPC$``, or a share only another
    machine mounts) the lookup is retried host-wide: a host that mounts *any*
    share almost always authenticates all of them with the same account, and
    that beats falling through to a generic credential file meant for the
    WebDAV channel.
    """
    found = _fstab_match(host, share)
    if not (found[0] or found[1]) and share:
        found = _fstab_match(host, "")
    return found


def _fstab_match(host: str, share: str) -> tuple[str, str]:
    global _HOST_CRED_NOTE_SHOWN
    if os.name == "nt":
        return "", ""
    # With no share (host-level calls like `shares`), accept any cifs entry for
    # this host; otherwise require the share itself or a subdirectory of it.
    target = f"//{host.lower()}/{share.lower()}" if share else f"//{host.lower()}/"
    for line in _read_lines(Path("/etc/fstab")):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split()
        if len(cols) < 4 or cols[2] != "cifs":
            continue
        src = cols[0].lower()
        if not (src == target.rstrip("/") or src.startswith(target)):
            continue
        m = re.search(r"credentials=([^,\s]+)", cols[3])
        if not m:
            continue
        user = pw = ""
        for entry in _read_lines(Path(m.group(1))):
            key, _, value = entry.partition("=")
            key = key.strip().lower()
            if key == "username":
                user = value.strip()
            elif key == "password":
                pw = value.strip()
        if user or pw:
            if not _HOST_CRED_NOTE_SHOWN:
                print(
                    "note: no MINAS_USER/MINAS_PASS configured; reusing this host's "
                    "existing cifs mount credentials for that share (set MINAS_USER / "
                    "MINAS_PASS or MINAS_CRED_FILE to override)",
                    file=sys.stderr,
                )
                _HOST_CRED_NOTE_SHOWN = True
            return user, pw
    return "", ""


def password_from_text(text: str) -> str:
    """Accept either a bare password or a KEY=VALUE credential/env file."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip().lower()] = value.strip().strip('"').strip("'")
    for key in ("minas_pass", "minas_password", "password"):
        if key in parsed:
            return parsed[key]
    if parsed:
        return ""          # looked like an env/cred file but carried no password
    return text.strip()    # a bare password file


def _password(host: str, share: str = "") -> tuple[str, str]:
    """Return (user, password) using the documented precedence; never logs.

    Order matters. This host mounts several NAS shares with **per-share**
    accounts (``/etc/fstab`` → ``/etc/samba/credentials/nas-*.cred``), while the
    shared ``credentials.env`` holds a single generic pair that may well belong
    to the *WebDAV* channel instead. So the share-specific mount credentials
    outrank the generic file:

    1. ``MINAS_PASSWORD_FILE`` — explicit password file
    2. environment ``MINAS_USER`` / ``MINAS_PASS`` — explicit
    3. this host's cifs mount credentials **for that share** — the account that
       actually owns the share
    4. shared ``$DSH_HOME/minasCred/credentials.env`` — generic fallback
    """
    pw_file = setting("MINAS_PASSWORD_FILE").strip()
    if pw_file:
        path = Path(pw_file)
        if not path.is_file():
            raise SmbError(f"MINAS_PASSWORD_FILE not readable: {pw_file}", 3)
        return setting("MINAS_USER"), password_from_text(
            path.read_text(encoding="utf-8", errors="replace"))

    env_user = os.environ.get("MINAS_USER", "")
    env_pw = os.environ.get("MINAS_PASSWORD") or os.environ.get("MINAS_PASS") or ""
    if env_user or env_pw:
        return env_user, env_pw

    # Share-specific mount credentials beat the generic shared file.
    host_user, host_pw = _fstab_credentials(host, share)
    if host_user or host_pw:
        return host_user, host_pw

    cred = _cred_file_env()
    return cred.get("MINAS_USER", ""), cred.get("MINAS_PASS") or cred.get("MINAS_PASSWORD") or ""


def auth_mode(host: str, share: str = "") -> str:
    """Describe how credentials will be supplied, without revealing any of them.

    Used by ``selftest``. It must reflect the *resolved* source, not just the
    environment — credentials may come from a file or from the host's cifs
    mounts, and reporting "anonymous" in that case is misleading.
    """
    user, _pw = _password(host, share)
    if not user:
        return "anonymous (-N)"
    if setting("MINAS_PASSWORD_FILE").strip():
        return "auth file (-A) from MINAS_PASSWORD_FILE"
    if os.environ.get("MINAS_USER") or os.environ.get("MINAS_PASSWORD") or os.environ.get("MINAS_PASS"):
        return "auth file (-A) from environment"
    if _fstab_credentials(host, share)[0]:
        return "auth file (-A) reusing this host's cifs mount credentials"
    return "auth file (-A) from the shared credentials file"


def _auth_args(host: str, share: str = "") -> list[str]:
    """Build ``-A <file>`` (preferred) or ``-N``. The file is 0600 and removed on exit."""
    global _AUTH_FILE
    user, password = _password(host, share)
    if not user:
        return ["-N"]
    if _AUTH_FILE and os.path.exists(_AUTH_FILE):
        return ["-A", _AUTH_FILE]
    fd, path = tempfile.mkstemp(prefix="minas-smb-", suffix=".auth")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"username = {user}\n")
            f.write(f"password = {password}\n")
    except Exception:
        os.close(fd) if not fd < 0 else None
        raise
    _AUTH_FILE = path
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    return ["-A", path]


# ---------------------------------------------------------------- invocation

def q(value: str) -> str:
    """Quote an smbclient ``-c`` operand, rejecting characters we cannot escape."""
    if any(ch in value for ch in ('"', ";", "\n", "\r")):
        raise SmbError(f"unsupported character in remote name: {value!r}", 1)
    return f'"{value}"'


def run(host: str, share: str, directory: str, commands: Iterable[str],
        timeout: int = 60) -> tuple[int, str]:
    """Run smbclient once. ``directory`` (if any) is set with ``-D``.

    Returns (returncode, combined output). Raises SmbError on a failed run.
    """
    exe = _require_smbclient()
    argv = [exe, f"//{host}/{share}"]
    if directory:
        argv += ["-D", directory]
    argv += _auth_args(host, share)
    argv += ["-m", "SMB3", "-t", str(max(timeout - 5, 5))]
    cmd_list = list(commands)
    if cmd_list:
        argv += ["-c", "; ".join(cmd_list)]
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SmbError(f"smbclient timed out after {timeout}s ({host}/{share})", 3)
    except FileNotFoundError:
        raise SmbError("smbclient not found", 1)
    out = (p.stdout or "") + (p.stderr or "")
    # A non-zero exit is normal for "no such file"; let callers decide.
    return p.returncode, out


def run_checked(host: str, share: str, directory: str,
                commands: Iterable[str], timeout: int = 60) -> str:
    """Like :func:`run`, but raises when the output carries an SMB error."""
    code, out = run(host, share, directory, commands, timeout)
    err = _error_line(out)
    if err:
        raise SmbError(f"{err} ({host}/{share}{('/' + directory) if directory else ''})",
                       classify(err))
    if code != 0:
        raise SmbError(f"smbclient exit {code} ({host}/{share})", 1)
    return out


def _error_line(out: str) -> str:
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("Anonymous login successful"):
            continue
        if line.startswith("NT_STATUS_") or "failed:" in line or line.startswith("session setup"):
            return line
    return ""


# ---------------------------------------------------------------- parsing

def _split(path: str) -> tuple[str, str, str]:
    from minas import parse_unc  # local import: minas.py imports us lazily
    return parse_unc(path)


def parse_listing(out: str) -> list[dict[str, Any]]:
    """Parse ``ls``/``dir`` output into [{'name','is_dir','size','mtime'}].

    ``.``/``..`` are dropped. Unparseable non-empty lines are collected; if
    *nothing* parsed we raise with the raw sample rather than return junk.
    """
    entries: list[dict[str, Any]] = []
    junk: list[str] = []
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("Anonymous login successful"):
            continue
        if stripped.startswith("NT_STATUS_") or "failed:" in stripped:
            continue
        if _LS_FOOTER.search(line):
            continue
        if stripped.replace("\\", "/").startswith(("listing ", "blocks of size")):
            continue
        m = _LS_ENTRY.match(stripped)
        if not m:
            junk.append(raw)
            continue
        name = m.group("name").strip()
        if name in (".", ".."):
            continue
        entries.append({
            "name": name,
            "is_dir": "D" in m.group("attr").upper(),
            "size": int(m.group("size")),
            "mtime": _parse_date(m.group("date")),
        })
    if junk and not entries:
        raise SmbError(
            "cannot parse smbclient listing output; raw sample:\n  "
            + "\n  ".join(junk[:3])
            + "\n(report this with `minas.py selftest <path> --raw`)",
            1,
        )
    if junk:
        print(f"warning: {len(junk)} unparsed listing line(s); first: {junk[0].strip()!r}",
              file=sys.stderr)
    return entries


def _parse_date(text: str) -> float:
    import time as _t
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b %d %H:%M %Y"):
        try:
            return _t.mktime(_t.strptime(text, fmt))
        except ValueError:
            continue
    return 0.0


def parse_free_space(out: str) -> tuple[int, int, int] | None:
    """Extract (total, used, free) bytes from the ``blocks of size`` footer."""
    m = _LS_FOOTER.search(out)
    if not m:
        return None
    used_blocks, blk, avail_blocks = (int(g) for g in m.groups())
    total = (used_blocks + avail_blocks) * blk
    return total, used_blocks * blk, avail_blocks * blk


def _cached_listing(host: str, share: str, directory: str) -> list[dict[str, Any]]:
    key = (host.lower(), share.lower(), directory.replace("\\", "/"))
    if key not in _ENTRY_CACHE:
        try:
            out = run_checked(host, share, directory, ["ls"])
        except SmbError as e:
            # Match pathlib semantics so the CLI's `remote_exists()` checks work
            # on both platforms: "no such path" is FileNotFoundError, while
            # network/auth failures stay SmbError (→ exit 3).
            if e.exit_code == 2:
                raise FileNotFoundError(f"\\\\{host}\\{share}\\{directory}") from e
            raise
        _ENTRY_CACHE[key] = parse_listing(out)
    return _ENTRY_CACHE[key]


def _probe_share(host: str, share: str) -> None:
    """Verify the share is reachable without listing it (cheap tree connect).

    ``quit`` forces the tree connect and costs ~50 ms, unlike ``ls`` which can
    enumerate a huge root just to answer ``stat()``.
    """
    try:
        run_checked(host, share, "", ["quit"])
    except SmbError as e:
        if e.exit_code == 2:
            raise FileNotFoundError(f"\\\\{host}\\{share}") from e
        raise


def invalidate(host: str, share: str, directory: str = "") -> None:
    """Drop cached listings after a write operation."""
    if directory:
        key = (host.lower(), share.lower(), directory.replace("\\", "/"))
        _ENTRY_CACHE.pop(key, None)
    else:
        prefix = (host.lower(), share.lower())
        for k in [k for k in _ENTRY_CACHE if k[:2] == prefix]:
            _ENTRY_CACHE.pop(k, None)


# ---------------------------------------------------------------- SmbPath

class _Stat:
    __slots__ = ("st_size", "st_mtime", "st_mode")

    def __init__(self, size: int, mtime: float, is_dir: bool):
        self.st_size = size
        self.st_mtime = mtime
        self.st_mode = 0o040000 if is_dir else 0o100000


class SmbPath:
    """Just enough of ``pathlib.Path`` for the minas CLI, over ``smbclient``."""

    __slots__ = ("_raw",)

    def __init__(self, raw: str):
        self._raw = raw.strip()

    # ---- representation -------------------------------------------------
    def __str__(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"SmbPath({self._raw!r})"

    def __fspath__(self) -> str:
        return self._raw

    def __eq__(self, other: object) -> bool:
        return str(self).lower() == str(other).lower()

    def __hash__(self) -> int:
        return hash(str(self).lower())

    def __truediv__(self, other: str) -> "SmbPath":
        return SmbPath(self._raw.rstrip("\\/") + "\\" + str(other))

    @property
    def _parts(self) -> tuple[str, str, str]:
        return _split(self._raw)

    @property
    def name(self) -> str:
        inner = self._parts[2]
        return inner.split("\\")[-1] if inner else self._parts[1]

    @property
    def parent(self) -> "SmbPath":
        host, share, inner = self._parts
        segs = [s for s in inner.split("\\") if s]
        if not segs:
            return self
        return SmbPath("\\\\" + "\\".join([host, share] + segs[:-1]))

    # ---- queries --------------------------------------------------------
    def exists(self) -> bool:
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def is_dir(self) -> bool:
        try:
            return self.stat().st_mode == 0o040000
        except FileNotFoundError:
            return False

    def is_file(self) -> bool:
        try:
            return self.stat().st_mode == 0o100000
        except FileNotFoundError:
            return False

    def stat(self) -> _Stat:
        host, share, inner = self._parts
        if not inner:
            # Share root: verify the share really exists (tree connect) before
            # claiming success, otherwise a typo'd share looks like a directory.
            _probe_share(host, share)
            return _Stat(0, 0.0, True)
        parent_inner, _, name = inner.rpartition("\\")
        listing = _cached_listing(host, share, parent_inner)
        for entry in listing:
            if entry["name"].lower() == name.lower():
                return _Stat(entry["size"], entry["mtime"], entry["is_dir"])
        raise FileNotFoundError(str(self))

    def iterdir(self):
        host, share, inner = self._parts
        for entry in _cached_listing(host, share, inner):
            child = SmbPath("\\\\" + "\\".join([host, share] + ([inner] if inner else []) + [entry["name"]]))
            yield child

    # ---- mutations ------------------------------------------------------
    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        host, share, inner = self._parts
        segs = [s for s in inner.split("\\") if s]
        if not segs:
            return
        if parents:
            # smbclient's mkdir does not create intermediate levels, so issue one
            # per prefix from the share root.
            directory = ""
            cmds = [f"mkdir {q(chr(92).join(segs[:i]))}" for i in range(1, len(segs) + 1)]
        else:
            # Mirror pathlib.Path.mkdir(parents=False): leaf only, parent must exist.
            directory = "\\".join(segs[:-1])
            cmds = [f"mkdir {q(segs[-1])}"]
        code, out = run(host, share, directory, cmds)
        err = _error_line(out)
        if err:
            # "already exists" is only an error when exist_ok is False.
            if "NT_STATUS_OBJECT_NAME_COLLISION" in err or "NT_STATUS_OBJECT_NAME_EXISTS" in err:
                if exist_ok or self.is_dir():
                    invalidate(host, share)
                    return
                raise SmbError(f"already exists: {self}", 1)
            raise SmbError(err, classify(err))
        invalidate(host, share)

    def unlink(self, missing_ok: bool = False) -> None:
        host, share, inner = self._parts
        parent_inner, _, name = inner.rpartition("\\")
        try:
            run_checked(host, share, parent_inner, [f"del {q(name)}"])
        except SmbError as e:
            if missing_ok and e.exit_code == 2:
                return
            raise
        invalidate(host, share, parent_inner)

    def rmdir(self) -> None:
        host, share, inner = self._parts
        parent_inner, _, name = inner.rpartition("\\")
        run_checked(host, share, parent_inner, [f"rmdir {q(name)}"])
        invalidate(host, share, parent_inner)

    def read_bytes(self) -> bytes:
        with tempfile.TemporaryDirectory(prefix="minas-get-") as tmp:
            local = Path(tmp) / (self.name or "blob")
            download(self, local)
            return local.read_bytes()

    def write_bytes(self, data: bytes) -> int:
        with tempfile.TemporaryDirectory(prefix="minas-put-") as tmp:
            local = Path(tmp) / (self.name or "blob")
            local.write_bytes(data)
            upload(local, self)
            return len(data)


# ---------------------------------------------------------------- transfers

def download(remote: Any, local: Path) -> None:
    """remote (SmbPath) → local file."""
    host, share, inner = _split(str(remote))
    parent_inner, _, name = inner.rpartition("\\")
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    cmds = [f'lcd {q(str(local.parent))}', f"get {q(name)} {q(local.name)}"]
    run_checked(host, share, parent_inner, cmds, timeout=600)


def upload(local: Path, remote: Any) -> None:
    """local file → remote (SmbPath)."""
    host, share, inner = _split(str(remote))
    parent_inner, _, name = inner.rpartition("\\")
    local = Path(local)
    SmbPath(str(remote).rsplit("\\", 1)[0] if parent_inner else
            "\\\\" + "\\".join(_split(str(remote))[:2])).mkdir(parents=True, exist_ok=True)
    cmds = [f'lcd {q(str(local.parent))}', f"put {q(local.name)} {q(name)}"]
    run_checked(host, share, parent_inner, cmds, timeout=600)
    invalidate(host, share, parent_inner)


# ---------------------------------------------------------------- fs_* shims

def _is_smb(obj: Any) -> bool:
    return isinstance(obj, SmbPath)


def fs_copy2(src: Any, dst: Any) -> None:
    """``shutil.copy2`` for local or remote endpoints."""
    if _is_smb(src) and _is_smb(dst):
        _server_copy_file(src, dst)
        return
    if _is_smb(src):
        download(src, Path(dst))
        return
    if _is_smb(dst):
        upload(Path(src), dst)
        return
    shutil.copy2(str(src), str(dst))


def _server_copy_file(src: SmbPath, dst: SmbPath) -> None:
    """Server-side copy via ``scopy``; falls back to a local round trip."""
    s_host, s_share, s_inner = _split(str(src))
    d_host, d_share, d_inner = _split(str(dst))
    s_parent, _, s_name = s_inner.rpartition("\\")
    if s_host.lower() == d_host.lower() and s_share.lower() == d_share.lower():
        try:
            run_checked(s_host, s_share, s_parent, [f"scopy {q(s_name)} {q(chr(92) + d_inner)}"])
            invalidate(s_host, s_share, d_inner.rpartition("\\")[0])
            return
        except SmbError as e:
            print(f"note: server-side scopy failed ({e}); using a local round trip",
                  file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="minas-cp-") as tmp:
        mid = Path(tmp) / (s_name or "blob")
        download(src, mid)
        upload(mid, dst)


def fs_copytree(src: Any, dst: Any, dirs_exist_ok: bool = False) -> None:
    """``shutil.copytree`` for local or remote endpoints."""
    if not (_is_smb(src) or _is_smb(dst)):
        shutil.copytree(str(src), str(dst), dirs_exist_ok=dirs_exist_ok)
        return
    if not dirs_exist_ok and dst.exists():
        raise FileExistsError(f"destination exists: {dst}")
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            fs_copytree(child, target, dirs_exist_ok=dirs_exist_ok)
        else:
            fs_copy2(child, target)


def fs_move(src: Any, dst: Any) -> None:
    """``shutil.move`` for local or remote endpoints."""
    if _is_smb(src) and _is_smb(dst):
        s_host, s_share, s_inner = _split(str(src))
        d_host, d_share, d_inner = _split(str(dst))
        s_parent, _, s_name = s_inner.rpartition("\\")
        if s_host.lower() == d_host.lower() and s_share.lower() == d_share.lower():
            try:
                run_checked(s_host, s_share, s_parent, [f"rename {q(s_name)} {q(chr(92) + d_inner)}"])
                invalidate(s_host, s_share)
                return
            except SmbError as e:
                print(f"note: server-side rename failed ({e}); copying then deleting",
                      file=sys.stderr)
        fs_copy2(src, dst)
        fs_rmtree(src)
        return
    if _is_smb(src) or _is_smb(dst):
        fs_copy2(src, dst)
        fs_rmtree(src)
        return
    shutil.move(str(src), str(dst))


def fs_rmtree(path: Any) -> None:
    """``shutil.rmtree`` for local or remote directories."""
    if not _is_smb(path):
        shutil.rmtree(str(path))
        return
    host, share, inner = _split(str(path))
    if not inner:
        raise SmbError("refusing to delete a share root", 1)
    parent_inner, _, name = inner.rpartition("\\")
    # deltree recurses; the trailing rmdir covers versions that keep the dir.
    # `run` returns (code, output) — not just output.
    _code, out = run(host, share, parent_inner, [f"deltree {q(name)}", f"rmdir {q(name)}"])
    err = _error_line(out)
    if err and not any(t in err for t in _NOTFOUND_ERRORS):
        raise SmbError(err, classify(err))
    invalidate(host, share)


def fs_disk_usage(path: Any) -> Any:
    """``shutil.disk_usage`` for local or remote paths.

    Over SMB the numbers come from smbclient's ``du`` footer
    (``N blocks of size S. M blocks available``), i.e. the *share/volume*.
    """
    if not _is_smb(path):
        return shutil.disk_usage(str(path))
    host, share, inner = _split(str(path))
    out = run_checked(host, share, inner, ["du"])
    usage = parse_free_space(out)
    if not usage:
        raise SmbError(
            "smbclient did not report free space (no 'blocks of size' footer); "
            "pool capacity over SMB is unavailable — use `minas-webdav df`",
            3,
        )
    total, used, free = usage
    return _DiskUsage(total, used, free)


# ---------------------------------------------------------------- shares

def list_shares(host: str) -> tuple[int, list[str]]:
    """Enumerate shares with ``smbclient -L``. Returns (exit_code, names)."""
    exe = _require_smbclient()
    argv = [exe, "-L", f"//{host}"] + _auth_args(host, "") + ["-m", "SMB3"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        return 3, []
    out = (p.stdout or "") + (p.stderr or "")
    names: list[str] = []
    seen_sep = False
    for line in out.splitlines():
        if re.match(r"^\s*-{3,}", line):
            seen_sep = True
            continue
        if not seen_sep:
            continue
        # Share rows are indented (tab or spaces). Trailing notes such as
        # "SMB1 disabled -- no workgroup available" start at column 0 and must
        # not be mistaken for share names.
        if not line[:1].isspace():
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "Sharename":
            names.append(parts[0])
    if not names:
        err = _error_line(out)
        if err:
            return classify(err), []
        if "Anonymous login successful" in out:
            return 0, []          # logged in, but no shares visible to us
        return 1, []
    return 0, names
