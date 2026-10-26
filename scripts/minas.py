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
"""MINAS Samba toolkit — agent file ops on Xiaomi Smart Storage over SMB/CIFS.

Primary transport is Samba (port 445). Remote paths are UNC on both platforms
(``//host/share/inner`` or ``\\\\host\\share\\inner``); how they are reached
differs:

* **Windows** — the OS resolves UNC paths natively; sessions are set up with
  ``net use`` and file ops go through ``pathlib``/``shutil``.
* **Linux / macOS** — there is no OS-level UNC, so the ``smbclient`` binary is
  used instead (see ``minas_smb.py``). Requires Samba's client to be installed
  (``apt install smbclient``). Nothing is mounted.

No secrets in source: configure via environment variables.

Environment:
  MINAS_HOST     NAS hostname or IP (e.g. 192.168.1.50 or minas.local)
  MINAS_SHARE    Share name to use as default root (optional)
  MINAS_USER     SMB username (optional if Windows already has a session)
  MINAS_PASSWORD_FILE  File holding the SMB password (preferred; wins even in
                 interactive terminals; password never goes on a process argv)
  MINAS_PASS     SMB password env fallback (last resort; a security warning is printed;
                 prefer MINAS_PASSWORD_FILE or the interactive prompt)
  MINAS_ROOT     Restrict every remote file op to this UNC prefix. Unset = any UNC path.
                 Remote paths must ALWAYS be UNC (\\\\host\\share\\...); local paths
                 (C:\\...) and "../" escapes outside MINAS_ROOT are rejected.

Examples:
  python minas.py shares
  python minas.py ls //NAS/share
  python minas.py ls //NAS/share --json
  python minas.py du //NAS/share/folder --max-depth 2
  python minas.py get //NAS/share/a.txt ./a.txt
  python minas.py put ./a.txt //NAS/share/a.txt
  python minas.py mkdir //NAS/share/work
  python minas.py mv //NAS/share/a.txt //NAS/share/work/a.txt
  python minas.py rm //NAS/share/work --recursive
  python minas.py find //NAS/share --name "*.mp4" --max-depth 3
  python minas.py selftest //NAS/share          # verify the SMB plumbing

Exit codes: 0 ok, 1 usage/error, 2 not found, 3 auth/network.

Platform notes (Linux):
  * `df` reports the share's total/used/free from smbclient's `du` footer. For
    the *pool* capacity of a MINAS device use `minas-webdav df` instead.
  * `mv`/`cp` prefer server-side `rename`/`scopy` and fall back to a local
    round trip when the server refuses; recursive copies walk the tree.
  * Credentials never touch argv: they go into a 0600 temp file passed via -A.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"

# Ensure the sibling backend module is importable when this file is executed
# directly (python scripts/minas.py) from any working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SMB_MOD: Any = None


def _smb() -> Any:
    """Lazy import of the POSIX backend (it imports helpers from this module)."""
    global _SMB_MOD
    if _SMB_MOD is None:
        import minas_smb  # noqa: WPS433 (deliberate lazy import)
        _SMB_MOD = minas_smb
    return _SMB_MOD


# --------------- path helpers ---------------

def norm_unc(path: str) -> str:
    """Normalize a UNC or local path to backslash form on Windows."""
    p = path.strip().replace("/", "\\")
    if p.startswith("\\\\"):
        return "\\\\" + p[2:].lstrip("\\")
    return p


def parse_unc(path: str) -> tuple[str, str, str]:
    """Return (host, share, inner). Accepts //host/share/inner or \\\\host\\share\\inner."""
    p = path.replace("/", "\\")
    if not p.startswith("\\\\"):
        raise ValueError(f"not a UNC path: {path}")
    parts = p[2:].split("\\")
    if len(parts) < 2:
        raise ValueError(f"UNC needs host and share: {path}")
    host, share = parts[0], parts[1]
    inner = "\\".join(parts[2:])
    return host, share, inner


def collapse_unc(path: str) -> str:
    """Lexically drop '.'/'..' segments in a normalized UNC path.

    '..' may not climb above \\host\\share (raises ValueError instead).
    """
    p = norm_unc(path)
    if not p.startswith("\\\\"):
        return p
    parts = p[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"UNC needs host and share: {path}")
    keep = parts[:2]
    for seg in parts[2:]:
        if seg in ("", "."):
            continue
        if seg == "..":
            if len(keep) <= 2:
                raise ValueError(f"path escapes share root: {path}")
            keep.pop()
            continue
        keep.append(seg)
    return "\\\\" + "\\".join(keep)


def safe_remote(path: str) -> str:
    """校验并规范化远端路径：必须是 UNC，且（若设置 MINAS_ROOT）落在其前缀之下。

    先做词法折叠去掉 ../，再比对前缀，防止用 .. 绕过 MINAS_ROOT。
    非 UNC 路径（如 C:\\...）一律拒绝。MINAS_ROOT 未设置时允许任意 UNC。
    """
    p = collapse_unc(path)
    if not p.startswith("\\\\"):
        raise ValueError(f"remote path must be UNC (\\\\host\\share\\...), got: {path}")
    root = setting("MINAS_ROOT").strip()
    if root:
        r = collapse_unc(root)
        if p.lower() != r.lower() and not p.lower().startswith(r.lower().rstrip("\\") + "\\"):
            raise ValueError(f"path outside MINAS_ROOT ({root}): {path}")
    return p


def to_local(path: str) -> Any:
    """Return a path object with the ``pathlib.Path`` API used by this CLI.

    Windows: a real ``pathlib.Path`` over the UNC path — the OS resolves it.
    POSIX:   ``minas_smb.SmbPath``, which shells out to ``smbclient``.
    """
    if IS_WINDOWS:
        return Path(norm_unc(path))
    return _smb().SmbPath(norm_unc(path))


# ---- shutil shims: same call sites work with local or remote endpoints -----

def fs_copy2(src: Any, dst: Any) -> None:
    if IS_WINDOWS:
        shutil.copy2(str(src), str(dst))
    else:
        _smb().fs_copy2(src, dst)


def fs_copytree(src: Any, dst: Any, dirs_exist_ok: bool = False) -> None:
    if IS_WINDOWS:
        shutil.copytree(str(src), str(dst), dirs_exist_ok=dirs_exist_ok)
    else:
        _smb().fs_copytree(src, dst, dirs_exist_ok=dirs_exist_ok)


def fs_move(src: Any, dst: Any) -> None:
    if IS_WINDOWS:
        shutil.move(str(src), str(dst))
    else:
        _smb().fs_move(src, dst)


def fs_rmtree(path: Any) -> None:
    if IS_WINDOWS:
        shutil.rmtree(str(path))
    else:
        _smb().fs_rmtree(path)


def fs_disk_usage(path: Any) -> Any:
    if IS_WINDOWS:
        return shutil.disk_usage(str(path))
    return _smb().fs_disk_usage(path)


# Windows winerror 中表示「主机/共享层不可达」的码；与「可达但内部路径不存在」
# (winerror 2) 区分开，前者走退出码 3（网络/权限），后者才是 2（路径不存在）
_NETWORK_WINERRORS = {51, 53, 67, 1203, 1231, 1236, 6118}


def remote_exists(p: Path, display: str) -> bool:
    """远端路径存在性检查：True=存在，False=可达但不存在（退出码 2）。

    主机/共享不可达（NAS 掉线、共享未开）时抛 OSError，
    由 main 映射为退出码 3，避免把网络故障误报成「路径不存在」。
    """
    try:
        p.stat()
        return True
    except FileNotFoundError as e:
        if getattr(e, "winerror", None) in _NETWORK_WINERRORS:
            raise OSError(f"cannot reach host/share (network/auth?): {display}") from e
        return False


# --------------- SMB session helpers ---------------

def setting(name: str, default: str = "") -> str:
    """Resolve a MINAS_* setting: environment, then the shared credentials file.

    Environment wins so explicit per-call overrides always take effect (and the
    offline unit tests stay hermetic). On POSIX the backend goes one step
    further and can reuse this host's existing cifs mount credentials.
    """
    value = os.environ.get(name)
    if value:
        return value
    return _smb().setting(name, default)


def smb_env() -> dict[str, str]:
    return {
        "host": setting("MINAS_HOST"),
        "share": setting("MINAS_SHARE"),
        "user": setting("MINAS_USER"),
        "password": setting("MINAS_PASS") or setting("MINAS_PASSWORD"),
    }


def _decode_net_bytes(data: bytes) -> str:
    """Decode `net` output. net.exe speaks the ANSI/OEM code page (cp936 on
    Chinese Windows), not UTF-8 — try the preferred encoding first."""
    if not data:
        return ""
    candidates = []
    pref = locale.getpreferredencoding(False)
    if pref:
        candidates.append(pref)
    candidates.extend(["cp936", "gbk", "utf-8"])
    seen: set[str] = set()
    for enc in candidates:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def run_net(args: list[str], timeout: int = 30, capture: bool = True) -> tuple[int, str, str]:
    exe = shutil.which("net") or shutil.which("net.exe") or "net"
    try:
        if not capture:
            # 交互模式：不接管 stdio，让 net use 的 "*" 密码提示直接显示给用户
            p = subprocess.run([exe, *args], timeout=None)
            return p.returncode, "", ""
        p = subprocess.run(
            [exe, *args],
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, _decode_net_bytes(p.stdout or b""), _decode_net_bytes(p.stderr or b"")
    except FileNotFoundError:
        return 127, "", "net command not found (use Windows or install Samba client)"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def resolve_password() -> str | None:
    """按优先级解析 SMB 密码，避免明文进入命令行。

    1. MINAS_PASSWORD_FILE → 显式配置的密码文件（交互与 agent 场景都优先）；
    2. 可交互控制台且未配密码文件 → 返回 "*"，交给 net use 的交互式密码提示；
    3. MINAS_PASSWORD / MINAS_PASS → 最后兼容，打印安全警告。
    """
    # 1) 密码文件（显式配置优先，agent/计划任务也走这里）
    pw_file = os.environ.get("MINAS_PASSWORD_FILE", "").strip()
    if pw_file:
        try:
            pw = Path(pw_file).read_text(encoding="utf-8").strip("\r\n")
        except OSError as e:
            print(f"cannot read MINAS_PASSWORD_FILE ({pw_file}): {e}", file=sys.stderr)
            return None
        print(
            f"password loaded from {pw_file}; restrict it to your own user only "
            f'(e.g. icacls "{pw_file}" /inheritance:r /grant:r %USERNAME%:F)',
            file=sys.stderr,
        )
        return pw
    # 2) 可交互控制台：交互提示（密码不进命令行）
    if IS_WINDOWS and sys.stdin.isatty() and sys.stdout.isatty():
        return "*"
    # 3) 环境变量（最后兼容，有泄露风险）
    pw = os.environ.get("MINAS_PASSWORD") or os.environ.get("MINAS_PASS") or ""
    if pw:
        print(
            "security warning: SMB password taken from environment "
            "(MINAS_PASSWORD/MINAS_PASS); prefer MINAS_PASSWORD_FILE or the "
            "interactive prompt",
            file=sys.stderr,
        )
        return pw
    return None


# 本进程内已成功的会话，避免 mv/cp 这类双路径命令反复 delete+重连
_sessions: set[str] = set()


def _wnet_add(target: str, user: str, password: str) -> tuple[int, str]:
    """Map a remote share via Win32 WNetAddConnection2W.

    Password is passed as an API argument (not a process command line), so it
    does not show up in `Get-Process` / Task Manager the way `net use ... pass` does.
    Returns (code, message). code 0 == success.
    """
    import ctypes
    from ctypes import wintypes

    class NETRESOURCE(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    add = mpr.WNetAddConnection2W
    add.argtypes = [ctypes.POINTER(NETRESOURCE), wintypes.LPWSTR, wintypes.LPWSTR, wintypes.DWORD]
    add.restype = wintypes.DWORD
    nr = NETRESOURCE()
    nr.dwType = 1  # RESOURCETYPE_DISK
    nr.lpLocalName = None
    nr.lpRemoteName = target
    code = add(ctypes.byref(nr), password or None, user or None, 0)
    if code == 0:
        return 0, ""
    return code, f"WNetAddConnection2W failed for {target}: error {code}"


def _map_remote(target: str, user: str, password: str | None, interactive: bool) -> tuple[int, str, str]:
    """Create an SMB session without putting the password on a process command line.

    interactive and password == "*" keeps the classic `net use ... *` prompt.
    A real password goes through WNetAddConnection2W (or PowerShell fallback).
    """
    if interactive or password == "*":
        args = ["use", target, f"/user:{user}", "*"]
        return run_net(args, capture=False)
    if not password:
        args = ["use", target, f"/user:{user}"]
        return run_net(args, capture=True)
    if IS_WINDOWS:
        code, msg = _wnet_add(target, user, password)
        if code == 0:
            return 0, "", ""
        # PowerShell fallback (still avoids `net use` argv); password via env, not cmdline.
        ps = (
            "$ErrorActionPreference='Stop';"
            "$sec=ConvertTo-SecureString $env:MINAS_MAP_PASS -AsPlainText -Force;"
            "New-SmbMapping -RemotePath $env:MINAS_MAP_TARGET "
            "-UserName $env:MINAS_MAP_USER -Password $sec -Persistent:$false "
            "| Out-Null"
        )
        env = os.environ.copy()
        env["MINAS_MAP_PASS"] = password
        env["MINAS_MAP_TARGET"] = target
        env["MINAS_MAP_USER"] = user
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                timeout=30,
                env=env,
            )
            if p.returncode == 0:
                return 0, "", ""
            err = _decode_net_bytes(p.stderr or b"") or _decode_net_bytes(p.stdout or b"") or msg
            return p.returncode, "", err
        except Exception as e:
            return 1, "", f"{type(e).__name__}: {e} ({msg})"
    # Non-Windows: last resort (password would be visible in process list)
    print(
        "security warning: password passed on the command line "
        "(no WNetAddConnection2 available on this platform)",
        file=sys.stderr,
    )
    return run_net(["use", target, f"/user:{user}", password], capture=True)


def ensure_session(host: str, share: str | None = None) -> None:
    """Best-effort: connect IPC$/share with env credentials if provided.

    POSIX has no persistent session to establish — ``smbclient`` authenticates
    on every invocation (see ``minas_smb._auth_args``) — so this is a no-op
    there and the CLI code can stay platform-agnostic.
    """
    if not IS_WINDOWS:
        return
    env = smb_env()
    user = env["user"]
    if not user:
        return  # rely on existing Windows session
    key = f"{host.lower()}\\{share or ''}"
    if key in _sessions:
        return
    password = resolve_password()
    interactive = password == "*"
    target = f"\\\\{host}\\{share}" if share else f"\\\\{host}\\IPC$"

    code, out, err = _map_remote(target, user, password, interactive)
    if code == 0:
        _sessions.add(key)
        return

    # 1219 = ERROR_SESSION_CREDENTIAL_CONFLICT: only then drop the old mapping.
    conflict = code == 1219 or "1219" in (err or "")
    if conflict:
        run_net(["use", target, "/delete", "/y"])
        code, out, err = _map_remote(target, user, password, interactive)
        if code == 0:
            _sessions.add(key)
            return

    if share:
        # try IPC$ only
        ipc = f"\\\\{host}\\IPC$"
        code2, _, err2 = _map_remote(ipc, user, password, interactive)
        if code2 != 0 and (code2 == 1219 or "1219" in (err2 or "")):
            run_net(["use", ipc, "/delete", "/y"])
            code2, _, _ = _map_remote(ipc, user, password, interactive)
        if code2 == 0:
            _sessions.add(key)


def list_shares(host: str) -> tuple[int, list[str]]:
    """List share names on host.

    Windows: ``net view`` (locale-dependent output, parsed leniently).
    POSIX:   ``smbclient -L`` (see ``minas_smb.list_shares``).

    Returns (exit_code, names). A non-zero exit code means the query failed
    (network/auth/permission) — callers must NOT treat that as "no shares".
    """
    if not IS_WINDOWS:
        return _smb().list_shares(host)
    ensure_session(host, None)
    code, out, err = run_net(["view", f"\\\\{host}"])
    text = out + "\n" + err
    names: list[str] = []
    if code == 0:
        # net view output is locale-dependent; grab tokens that look like share rows.
        # Prefer lines after the dashed separator.
        lines = text.splitlines()
        seen_sep = False
        status_re = re.compile(
            r"(命令成功完成|The command completed|successfully\.?$|"
            r"什么都没有列出|There are no entries|列出 .* 的共享)",
            re.I,
        )
        for line in lines:
            if re.match(r"^-{3,}", line.strip()):
                seen_sep = True
                continue
            if not seen_sep:
                continue
            if not line.strip() or status_re.search(line):
                continue
            # columns: Share name  Type  Used as  Comment
            m = re.match(r"^(\S+(?:\s\S+)*?)\s{2,}(\S+)\s{2,}(.*?)$", line)
            if m:
                names.append(m.group(1).strip())
                continue
            # fallback: first token
            toks = line.split()
            if toks:
                names.append(toks[0])
        # unique preserve order; drop locale header noise tokens
        noise = {
            "the", "command", "completed", "successfully", "there",
            "命令", "成功", "完成", "的", "共享名", "资源", "注释",
            "什么", "都不", "是", "有", "列出", "主机",
        }
        out_names = []
        for n in names:
            if not n or n in out_names:
                continue
            if n.lower() in noise:
                continue
            out_names.append(n)
        names = out_names
    return code, names


# --------------- file ops ---------------

def cmd_shares(_client: Any, args: argparse.Namespace) -> int:
    host = args.host or smb_env()["host"]
    if not host:
        print("set MINAS_HOST or pass --host", file=sys.stderr)
        return 1
    code, names = list_shares(host)
    if code != 0:
        # 查询失败 ≠ 没有共享：必须以非 0 退出码暴露给 agent
        if code == 127:
            print("net command not found (install Samba's smbclient on POSIX)",
                  file=sys.stderr)
            return 1
        print(f"share enumeration failed for {host} (exit {code}); "
              f"check network/credentials", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps({"host": host, "shares": names}, ensure_ascii=False, indent=2))
    else:
        print(f"host: {host}")
        for n in names:
            print(f"  {n}")
    return 0


def cmd_ls(_client: Any, args: argparse.Namespace) -> int:
    path = safe_remote(args.path)
    ensure_session(*parse_unc(path)[:2])
    p = to_local(path)
    if not remote_exists(p, path):
        print(f"not found: {path}", file=sys.stderr)
        return 2
    if p.is_file():
        items = [{"name": p.name, "path": str(p), "size": p.stat().st_size, "is_dir": False}]
    else:
        items = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                st = child.stat()
                size = st.st_size if child.is_file() else 0
                modified = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
                    if child.is_file()
                    else ""
                )
            except OSError:
                # stat 失败：不要复用上一轮的 st，否则会给出错误的 mtime
                size = 0
                modified = ""
            items.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "size": size,
                    "is_dir": child.is_dir(),
                    "modified": modified,
                }
            )
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    for it in items:
        kind = "d" if it["is_dir"] else "-"
        size = fmt_size(it["size"]) if not it["is_dir"] else ""
        print(f"{kind} {size:>10}  {it['name']}")
    return 0


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def dir_size(path: Path, max_depth: int, depth: int = 0) -> tuple[int, int]:
    total = 0
    count = 0
    try:
        for child in path.iterdir():
            if child.is_dir():
                if depth < max_depth:
                    t, c = dir_size(child, max_depth, depth + 1)
                    total += t
                    count += c
            else:
                try:
                    total += child.stat().st_size
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total, count


def cmd_du(_client: Any, args: argparse.Namespace) -> int:
    path = safe_remote(args.path)
    ensure_session(*parse_unc(path)[:2])
    p = to_local(path)
    if not remote_exists(p, path):
        print(f"not found: {path}", file=sys.stderr)
        return 2
    rows = []
    if p.is_file():
        rows.append((p.name, p.stat().st_size, 1, False))
    else:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.is_dir():
                t, c = dir_size(child, args.max_depth)
                rows.append((child.name, t, c, True))
            else:
                try:
                    rows.append((child.name, child.stat().st_size, 1, False))
                except OSError:
                    rows.append((child.name, 0, 0, False))
        rows.sort(key=lambda r: -r[1])
    if args.json:
        print(
            json.dumps(
                [{"name": n, "bytes": b, "files": c, "is_dir": d} for n, b, c, d in rows],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    grand = 0
    for name, total, count, is_dir in rows:
        grand += total
        mark = "/" if is_dir else ""
        print(f"{fmt_size(total):>10}  {count:>6}  {name}{mark}")
    print("-" * 40)
    print(f"{fmt_size(grand):>10}  TOTAL")
    return 0


def cmd_df(_client: Any, args: argparse.Namespace) -> int:
    path = args.path
    if not path:
        env = smb_env()
        if env["host"] and env["share"]:
            path = f"//{env['host']}/{env['share']}"
        else:
            print("pass a UNC path or set MINAS_HOST and MINAS_SHARE", file=sys.stderr)
            return 1
    path = safe_remote(path)
    ensure_session(*parse_unc(path)[:2])
    p = to_local(path)
    if not remote_exists(p, path):
        print(f"not found: {path}", file=sys.stderr)
        return 2
    try:
        usage = fs_disk_usage(p if p.is_dir() else p.parent)
        info = {
            "path": str(p),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total_human": fmt_size(usage.total),
            "used_human": fmt_size(usage.used),
            "free_human": fmt_size(usage.free),
        }
    except OSError as e:
        print(f"disk_usage failed: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"path:  {info['path']}")
        print(f"total: {info['total_human']}  used: {info['used_human']}  free: {info['free_human']}")
    return 0


def cmd_get(_client: Any, args: argparse.Namespace) -> int:
    remote_path = safe_remote(args.remote)
    ensure_session(*parse_unc(remote_path)[:2])
    remote = to_local(remote_path)
    local = Path(args.local)
    if not remote_exists(remote, args.remote):
        print(f"not found: {args.remote}", file=sys.stderr)
        return 2
    if local.is_dir():
        local = local / remote.name
    local.parent.mkdir(parents=True, exist_ok=True)
    fs_copy2(remote, local)
    print(f"saved {local} ({fmt_size(local.stat().st_size)})")
    return 0


def cmd_put(_client: Any, args: argparse.Namespace) -> int:
    local = Path(args.local)
    if not local.exists():
        print(f"local not found: {local}", file=sys.stderr)
        return 2
    if local.is_dir():
        print("local is a directory; copy files one by one or use robocopy", file=sys.stderr)
        return 1
    remote_path = safe_remote(args.remote)
    ensure_session(*parse_unc(remote_path)[:2])
    remote = to_local(remote_path)
    remote.parent.mkdir(parents=True, exist_ok=True)
    fs_copy2(local, remote)
    print(f"uploaded {local} -> {remote} ({fmt_size(local.stat().st_size)})")
    return 0


def cmd_cat(_client: Any, args: argparse.Namespace) -> int:
    path = safe_remote(args.path)
    ensure_session(*parse_unc(path)[:2])
    remote = to_local(path)
    if not remote_exists(remote, args.path):
        print(f"not found: {args.path}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(remote.read_bytes())
    return 0


def cmd_mkdir(_client: Any, args: argparse.Namespace) -> int:
    path = safe_remote(args.path)
    ensure_session(*parse_unc(path)[:2])
    remote = to_local(path)
    remote.mkdir(parents=True, exist_ok=True)
    print(f"mkdir ok: {remote}")
    return 0


def cmd_rm(_client: Any, args: argparse.Namespace) -> int:
    # 递归删除严格走 safe_remote：非 UNC 或越过 MINAS_ROOT 一律拒绝
    path = safe_remote(args.path)
    ensure_session(*parse_unc(path)[:2])
    remote = to_local(path)
    if not remote_exists(remote, args.path):
        print(f"not found: {args.path}", file=sys.stderr)
        return 2
    if remote.is_dir():
        if not args.recursive:
            print("refusing to delete directory without --recursive", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"would remove dir: {remote}")
            return 0
        fs_rmtree(remote)
    else:
        if args.dry_run:
            print(f"would remove file: {remote}")
            return 0
        remote.unlink()
    print(f"removed: {remote}")
    return 0


def cmd_mv(_client: Any, args: argparse.Namespace) -> int:
    src_path = safe_remote(args.src)
    dst_path = safe_remote(args.dst)
    ensure_session(*parse_unc(src_path)[:2])
    ensure_session(*parse_unc(dst_path)[:2])
    src = to_local(src_path)
    dst = to_local(dst_path)
    if not remote_exists(src, args.src):
        print(f"not found: {args.src}", file=sys.stderr)
        return 2
    if remote_exists(dst, args.dst) and not args.force:
        print(f"destination exists (use --force): {dst}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"would move {src} -> {dst}")
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    fs_move(src, dst)
    print(f"moved {src} -> {dst}")
    return 0


def cmd_cp(_client: Any, args: argparse.Namespace) -> int:
    src_path = safe_remote(args.src)
    dst_path = safe_remote(args.dst)
    ensure_session(*parse_unc(src_path)[:2])
    ensure_session(*parse_unc(dst_path)[:2])
    src = to_local(src_path)
    dst = to_local(dst_path)
    if not remote_exists(src, args.src):
        print(f"not found: {args.src}", file=sys.stderr)
        return 2
    dst_exists = remote_exists(dst, args.dst)
    if dst_exists and not args.force:
        print(f"destination exists (use --force): {dst}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"would copy {src} -> {dst}")
        return 0
    if src.is_dir():
        # dirs_exist_ok so --force can overwrite an existing directory tree
        fs_copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fs_copy2(src, dst)
    print(f"copied {src} -> {dst}")
    return 0


def cmd_find(_client: Any, args: argparse.Namespace) -> int:
    root_path = safe_remote(args.path)
    ensure_session(*parse_unc(root_path)[:2])
    root = to_local(root_path)
    if not remote_exists(root, args.path):
        print(f"not found: {args.path}", file=sys.stderr)
        return 2
    found = []

    def rec(path: Path, depth: int) -> None:
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            is_dir = child.is_dir()
            name_ok = True
            if args.name:
                name_ok = fnmatch.fnmatch(child.name, args.name)
            type_ok = True
            if args.files and is_dir:
                type_ok = False
            if args.dirs and not is_dir:
                type_ok = False
            if name_ok and type_ok:
                size = 0
                if not is_dir:
                    try:
                        size = child.stat().st_size
                    except OSError:
                        pass
                found.append({"path": str(child), "name": child.name, "is_dir": is_dir, "size": size})
            if is_dir and depth < args.max_depth:
                rec(child, depth + 1)

    rec(root, 0)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
    else:
        for it in found:
            mark = "/" if it["is_dir"] else ""
            print(f"{fmt_size(it['size']):>10}  {it['path']}{mark}")
    return 0


def cmd_tree(_client: Any, args: argparse.Namespace) -> int:
    root_path = safe_remote(args.path)
    ensure_session(*parse_unc(root_path)[:2])
    root = to_local(root_path)
    if not remote_exists(root, args.path):
        print(f"not found: {args.path}", file=sys.stderr)
        return 2

    def rec(path: Path, prefix: str, depth: int) -> None:
        try:
            children = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError:
            return
        for i, child in enumerate(children):
            last = i == len(children) - 1
            branch = "└── " if last else "├── "
            extra = "/" if child.is_dir() else ""
            if not child.is_dir():
                try:
                    extra = f"  ({fmt_size(child.stat().st_size)})"
                except OSError:
                    extra = ""
            print(f"{prefix}{branch}{child.name}{extra}")
            if child.is_dir() and depth < args.max_depth:
                rec(child, prefix + ("    " if last else "│   "), depth + 1)

    print(root)
    rec(root, "", 0)
    return 0


def cmd_selftest(_client: Any, args: argparse.Namespace) -> int:
    """Verify the SMB plumbing against a real share. Read-only unless --raw.

    Exists because the POSIX backend parses ``smbclient`` text output; run this
    once after configuring credentials to confirm listing/stat/disk usage work
    against your own server before trusting the other commands.
    """
    path = safe_remote(args.path)
    host, share, inner = parse_unc(path)
    where = f"//{host}/{share}" + (f"/{inner}" if inner else "")
    print(f"platform : {'windows' if IS_WINDOWS else 'posix'}")
    if IS_WINDOWS:
        print("backend  : net use + pathlib (OS resolves UNC)")
    else:
        m = _smb()
        print(f"backend  : smbclient ({m.SMBCLIENT or 'NOT FOUND — install smbclient'})")
        print(f"auth     : {m.auth_mode(host, share)}")
    print(f"target   : {where}")
    if not IS_WINDOWS and args.raw:
        code, out = _smb().run(host, share, inner, ["ls"])
        print(f"--- raw smbclient output (exit {code}) ---")
        print(out.rstrip())
        print("--- end raw ---")
    p = to_local(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        print("stat     : NOT FOUND")
        return 2
    print(f"stat     : is_dir={st.st_mode == 0o040000} size={st.st_size} mtime={int(st.st_mtime)}")
    if p.is_dir():
        kids = list(p.iterdir())
        print(f"iterdir  : {len(kids)} entr{'y' if len(kids) == 1 else 'ies'}")
        for k in kids[:5]:
            print(f"           {k.name}")
        try:
            u = fs_disk_usage(p)
            print(f"disk     : total={u.total} used={u.used} free={u.free}")
        except OSError as e:
            print(f"disk     : unavailable ({e})")
    print("selftest : OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minas", description="Xiaomi Smart Storage Samba CLI for agents")
    p.add_argument("--host", default=os.environ.get("MINAS_HOST", ""), help="NAS host/IP")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("shares", help="list SMB shares")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--host", default=os.environ.get("MINAS_HOST", ""), help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_shares)

    sp = sub.add_parser("ls")
    sp.add_argument("path")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--host", default=os.environ.get("MINAS_HOST", ""), help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("du")
    sp.add_argument("path")
    sp.add_argument("--max-depth", type=int, default=2)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--host", default=os.environ.get("MINAS_HOST", ""), help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_du)

    sp = sub.add_parser("tree")
    sp.add_argument("path")
    sp.add_argument("--max-depth", type=int, default=2)
    sp.add_argument("--host", default=os.environ.get("MINAS_HOST", ""), help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_tree)

    sp = sub.add_parser("df", help="disk usage of a UNC path")
    sp.add_argument("path", nargs="?", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_df)

    sp = sub.add_parser("get", help="download file")
    sp.add_argument("remote")
    sp.add_argument("local")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("put", help="upload file")
    sp.add_argument("local")
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_put)

    sp = sub.add_parser("cat", help="print file")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_cat)

    sp = sub.add_parser("mkdir", help="create directory")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_mkdir)

    sp = sub.add_parser("rm", help="delete file/dir")
    sp.add_argument("path")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="print what would be removed, change nothing")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("mv", help="move/rename")
    sp.add_argument("src")
    sp.add_argument("dst")
    sp.add_argument("-f", "--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="print the planned move, change nothing")
    sp.set_defaults(func=cmd_mv)

    sp = sub.add_parser("cp", help="copy")
    sp.add_argument("src")
    sp.add_argument("dst")
    sp.add_argument("-f", "--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="print the planned copy, change nothing")
    sp.set_defaults(func=cmd_cp)

    sp = sub.add_parser("find", help="find by name pattern")
    sp.add_argument("path")
    sp.add_argument("--name")
    sp.add_argument("--max-depth", type=int, default=3)
    sp.add_argument("--files", action="store_true")
    sp.add_argument("--dirs", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("selftest", help="verify the SMB plumbing against a real share")
    sp.add_argument("path")
    sp.add_argument("--raw", action="store_true", help="dump raw smbclient output (posix)")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(None, args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"permission denied: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        # minas_smb.SmbError carries the CLI exit code (2 = not found, 3 = auth/net)
        ec = getattr(e, "exit_code", None)
        if ec is not None:
            print(str(e), file=sys.stderr)
            return int(ec)
        print(f"os error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
