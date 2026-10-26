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
"""minas CLI 单测：路径安全约束与退出码（纯离线，不触网）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import minas  # noqa: E402


def _net_file_not_found() -> FileNotFoundError:
    """模拟 Windows 上主机/共享不可达时 stat 抛出的异常（errno=2 + winerror=53）。"""
    err = FileNotFoundError(errno.ENOENT, "The network path was not found.")
    err.winerror = 53
    return err


class TestPathHelpers(unittest.TestCase):
    def test_norm_unc_converts_slashes(self):
        self.assertEqual(minas.norm_unc("//host/share/a"), "\\\\host\\share\\a")

    def test_norm_unc_keeps_local_path(self):
        self.assertEqual(minas.norm_unc("C:/Windows"), "C:\\Windows")

    def test_parse_unc_ok(self):
        self.assertEqual(minas.parse_unc("//host/share/a/b"), ("host", "share", "a\\b"))

    def test_parse_unc_rejects_local(self):
        with self.assertRaises(ValueError):
            minas.parse_unc("C:\\Windows")

    def test_parse_unc_needs_share(self):
        with self.assertRaises(ValueError):
            minas.parse_unc("\\\\host")

    def test_collapse_dots(self):
        self.assertEqual(
            minas.collapse_unc("//host/share/a/./b/../c"), "\\\\host\\share\\a\\c"
        )

    def test_collapse_cannot_escape_share(self):
        with self.assertRaises(ValueError):
            minas.collapse_unc("//host/share/../other")
        with self.assertRaises(ValueError):
            minas.collapse_unc("//host/share/a/../../..")


class TestSafeRemote(unittest.TestCase):
    def test_rejects_local_and_relative_paths(self):
        for bad in ("C:\\Windows\\Temp", "D:/data/file", "/pool0/data", "relative/path"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                minas.safe_remote(bad)

    def test_allows_any_unc_without_root(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": ""}):
            self.assertEqual(minas.safe_remote("//nas/share/x"), "\\\\nas\\share\\x")

    def test_normalizes_dotdot_before_root_check(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            self.assertEqual(
                minas.safe_remote("//nas/share/a/../b"), "\\\\nas\\share\\b"
            )

    def test_root_inside_ok_case_insensitive(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            self.assertEqual(
                minas.safe_remote("\\\\NAS\\SHARE\\photos"), "\\\\NAS\\SHARE\\photos"
            )

    def test_root_equal_ok(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            self.assertEqual(minas.safe_remote("//nas/share"), "\\\\nas\\share")

    def test_outside_root_rejected(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            with self.assertRaises(ValueError):
                minas.safe_remote("//nas/other")

    def test_sibling_prefix_rejected(self):
        # \\nas\share2 不是 \\nas\share 的子路径，必须拒绝
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            with self.assertRaises(ValueError):
                minas.safe_remote("//nas/share2/x")

    def test_dotdot_escape_rejected(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            with self.assertRaises(ValueError):
                minas.safe_remote("//nas/share/../other")

    def test_root_with_trailing_backslash(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share\\"}):
            self.assertEqual(minas.safe_remote("//nas/share/x"), "\\\\nas\\share\\x")


class TestRemoteExists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = Path(self.tmp) / "a.txt"
        self.file.write_text("x", encoding="utf-8")

    def test_existing_path_true(self):
        self.assertTrue(minas.remote_exists(self.file, str(self.file)))

    def test_missing_local_path_false(self):
        self.assertFalse(minas.remote_exists(Path(self.tmp) / "nope", "nope"))

    def test_unreachable_host_raises(self):
        class FakePath:
            def stat(self):
                raise _net_file_not_found()

        with self.assertRaises(OSError):
            minas.remote_exists(FakePath(), "\\\\nas\\share")


class TestFmtSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(minas.fmt_size(0), "0.0B")
        self.assertEqual(minas.fmt_size(1024), "1.0KB")
        self.assertEqual(minas.fmt_size(1536), "1.5KB")
        self.assertEqual(minas.fmt_size(1024**3), "1.0GB")


class TestDirSize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_respects_max_depth(self):
        root = Path(self.tmp)
        (root / "f1.txt").write_text("12345", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "f2.txt").write_text("1234567890", encoding="utf-8")
        (root / "sub" / "deep").mkdir()
        (root / "sub" / "deep" / "f3.txt").write_text("1", encoding="utf-8")

        total0, count0 = minas.dir_size(root, max_depth=0)
        self.assertEqual((total0, count0), (5, 1))
        total1, count1 = minas.dir_size(root, max_depth=1)
        self.assertEqual((total1, count1), (15, 2))
        total2, count2 = minas.dir_size(root, max_depth=5)
        self.assertEqual((total2, count2), (16, 3))


class TestParser(unittest.TestCase):
    def test_dry_run_flags(self):
        args = minas.build_parser().parse_args(["rm", "//h/s/x", "-r", "--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.recursive)
        args = minas.build_parser().parse_args(["mv", "//h/s/a", "//h/s/b", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_max_depth_defaults(self):
        self.assertEqual(minas.build_parser().parse_args(["du", "//h/s"]).max_depth, 2)
        self.assertEqual(minas.build_parser().parse_args(["find", "//h/s"]).max_depth, 3)


class TestExitCodes(unittest.TestCase):
    def test_local_path_rejected(self):
        self.assertEqual(minas.main(["ls", "C:\\Windows"]), 1)
        self.assertEqual(minas.main(["rm", "C:\\Windows\\Temp", "-r"]), 1)

    def test_root_violation_maps_to_1(self):
        with mock.patch.dict(os.environ, {"MINAS_ROOT": "\\\\nas\\share"}):
            self.assertEqual(minas.main(["ls", "//nas/other"]), 1)

    def test_shares_without_host(self):
        with mock.patch.dict(os.environ, {"MINAS_HOST": ""}):
            self.assertEqual(minas.main(["shares"]), 1)

    def test_shares_failure_returns_3(self):
        # Pin the Windows backend: these two cases cover the `net view` parsing
        # path, which must stay tested even when the suite runs on POSIX.
        with mock.patch.object(minas, "IS_WINDOWS", True), mock.patch.object(
            minas, "ensure_session"
        ), mock.patch.object(
            minas, "run_net", return_value=(2, "", "System error 53")
        ), mock.patch.dict(os.environ, {"MINAS_HOST": "nas"}):
            self.assertEqual(minas.main(["shares"]), 3)

    def test_shares_success_parses_rows(self):
        sample = (
            "Share name     Type     Used as  Comment\n"
            "--------------------------------------\n"
            "public         Disk\n"
            "data-home      Disk\n"
            "The command completed successfully.\n"
        )
        buf = io.StringIO()
        with mock.patch.object(minas, "IS_WINDOWS", True), mock.patch.object(
            minas, "ensure_session"
        ), mock.patch.object(
            minas, "run_net", return_value=(0, sample, "")
        ), mock.patch.dict(os.environ, {"MINAS_HOST": "nas"}), redirect_stdout(buf):
            rc = minas.main(["shares", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["shares"], ["public", "data-home"])


class TestPosixSmbBackend(unittest.TestCase):
    """POSIX 后端：smbclient 输出解析与凭据处理（全部离线，不联网）。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import minas_smb  # noqa: WPS433

        self.smb = minas_smb
        self.addCleanup(self.smb._ENTRY_CACHE.clear)

    LISTING = (
        "Anonymous login successful\n"
        "  .                                   D        0  Sat Oct 11 14:00:00 2026\n"
        "  ..                                  D        0  Sat Oct 11 14:00:00 2026\n"
        "  hello world.txt                     A     1234  Sat Oct 11 14:00:00 2026\n"
        "  subdir                              D        0  Sat Oct 11 14:00:00 2026\n"
        "  中文目录名                          D        0  Sat Oct 11 14:00:00 2026\n"
        "\n\t\t12345 blocks of size 1024. 6789 blocks available\n"
    )

    def test_parse_listing_shapes(self):
        got = self.smb.parse_listing(self.LISTING)
        names = [e["name"] for e in got]
        self.assertEqual(names, ["hello world.txt", "subdir", "中文目录名"])
        self.assertEqual(got[0]["size"], 1234)
        self.assertFalse(got[0]["is_dir"])
        self.assertTrue(got[1]["is_dir"])

    def test_parse_listing_rejects_garbage_loudly(self):
        """无法解析时必须报错，绝不静默返回错误元数据。"""
        with self.assertRaises(self.smb.SmbError):
            self.smb.parse_listing("  ??? nothing like a listing ???\n")

    def test_parse_free_space(self):
        total, used, free = self.smb.parse_free_space(self.LISTING)
        self.assertEqual(total, (12345 + 6789) * 1024)
        self.assertEqual(used, 12345 * 1024)
        self.assertEqual(free, 6789 * 1024)

    def test_classify_exit_codes(self):
        self.assertEqual(self.smb.classify("session setup failed: NT_STATUS_LOGON_FAILURE"), 3)
        self.assertEqual(self.smb.classify("tree connect failed: NT_STATUS_BAD_NETWORK_NAME"), 2)
        self.assertEqual(self.smb.classify("NT_STATUS_OBJECT_NAME_NOT_FOUND"), 2)
        self.assertEqual(self.smb.classify("something else"), 1)

    def test_password_never_reaches_argv(self):
        """凭据必须走 0600 的 -A 文件，不能出现在命令行。"""
        seen = {}

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            seen["authfile"] = argv[argv.index("-A") + 1] if "-A" in argv else None
            return R()

        with mock.patch.dict(
            os.environ, {"MINAS_USER": "u", "MINAS_PASS": "TOPSECRET"}, clear=False
        ), mock.patch.object(self.smb.subprocess, "run", fake_run), mock.patch.object(
            self.smb, "SMBCLIENT", "/usr/bin/smbclient"
        ), mock.patch.object(self.smb, "_AUTH_FILE", None):
            self.smb.run("host", "share", "", ["ls"])
        argv, authfile = seen["argv"], seen["authfile"]
        self.assertNotIn("TOPSECRET", " ".join(argv))
        self.assertIn("-A", argv)
        self.assertTrue(authfile)
        self.assertEqual(oct(os.stat(authfile).st_mode & 0o777), "0o600")
        os.unlink(authfile)

    def test_anonymous_when_no_user(self):
        """没有任何可用凭据时必须匿名（-N）。

        显式隔离三类凭据来源，否则会读到本机真实的 credentials.env 而不再绝缘。
        """
        seen = {}

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return R()

        env = {k: v for k, v in os.environ.items() if k != "MINAS_USER"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            self.smb.subprocess, "run", fake_run
        ), mock.patch.object(self.smb, "SMBCLIENT", "/usr/bin/smbclient"), mock.patch.object(
            self.smb, "_cred_file_env", lambda: {}
        ), mock.patch.object(
            self.smb, "_fstab_credentials", lambda *a, **k: ("", "")
        ):
            self.smb.run("host", "share", "", ["ls"])
        argv = seen["argv"]
        self.assertIn("-N", argv)
        self.assertNotIn("-A", argv)

    def test_share_mount_credentials_beat_generic_file(self):
        """本机按共享挂载的凭据优先于通用凭据文件。

        本机 credentials.env 放的是 WebDAV 那套账号，而每个 SMB 共享各有自己的
        挂载账号；通用文件若抢先命中，SMB 就会拿错账号 → LOGON_FAILURE。
        """
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MINAS_")}
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(
            self.smb, "_cred_file_env",
            lambda: {"MINAS_USER": "webdav-user", "MINAS_PASS": "webdav-pass"},
        ), mock.patch.object(
            self.smb, "_fstab_credentials", lambda host, share: ("smb-user", "smb-pass")
        ):
            self.assertEqual(self.smb._password("nas", "share"), ("smb-user", "smb-pass"))

    def test_generic_file_used_when_share_not_mounted(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MINAS_")}
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(
            self.smb, "_cred_file_env", lambda: {"MINAS_USER": "u", "MINAS_PASS": "p"}
        ), mock.patch.object(
            self.smb, "_fstab_credentials", lambda host, share: ("", "")
        ):
            self.assertEqual(self.smb._password("nas", "share"), ("u", "p"))

    def test_environment_beats_mount_and_file(self):
        with mock.patch.dict(
            os.environ, {"MINAS_USER": "env-u", "MINAS_PASS": "env-p"}, clear=False
        ), mock.patch.object(
            self.smb, "_cred_file_env",
            lambda: {"MINAS_USER": "file-u", "MINAS_PASS": "file-p"},
        ), mock.patch.object(
            self.smb, "_fstab_credentials", lambda host, share: ("mnt-u", "mnt-p")
        ):
            self.assertEqual(self.smb._password("nas", "share"), ("env-u", "env-p"))

    def test_list_shares_parsing_skips_trailing_note(self):
        """共享行是缩进的；尾部提示（如 SMB1 disabled…）不能被当成共享名。"""
        sample = (
            "Anonymous login successful\n\n"
            "\tSharename       Type      Comment\n"
            "\t---------       ----      -------\n"
            "\tIPC$            IPC       IPC Service (MINAS)\n"
            "\tbackup          Disk      Z drive\n"
            "SMB1 disabled -- no workgroup available\n"
        )

        class R:
            returncode = 0
            stdout = sample
            stderr = ""

        with mock.patch.object(self.smb.subprocess, "run", lambda *a, **k: R()), \
                mock.patch.object(self.smb, "SMBCLIENT", "/usr/bin/smbclient"):
            code, names = self.smb.list_shares("nas")
        self.assertEqual(code, 0)
        self.assertEqual(names, ["IPC$", "backup"])


class TestDryRunSafety(unittest.TestCase):
    """dry-run 绝不能改动文件系统。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 绕过 UNC 校验与会话建立，让命令作用在本地临时目录上；
        # to_local 也一并替换，避免 norm_unc 把 POSIX 路径的 / 改成 \
        self.patches = [
            mock.patch.object(minas, "safe_remote", side_effect=lambda p: p),
            mock.patch.object(minas, "to_local", side_effect=lambda p: Path(p)),
            mock.patch.object(minas, "parse_unc", return_value=("h", "s", "")),
            mock.patch.dict(os.environ, {"MINAS_USER": ""}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_rm_dry_run_keeps_file(self):
        f = Path(self.tmp) / "victim.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(minas.main(["rm", str(f), "--dry-run"]), 0)
        self.assertTrue(f.exists())

    def test_rm_real_deletes(self):
        f = Path(self.tmp) / "victim.txt"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(minas.main(["rm", str(f)]), 0)
        self.assertFalse(f.exists())

    def test_rm_dir_requires_recursive(self):
        d = Path(self.tmp) / "dir"
        d.mkdir()
        self.assertEqual(minas.main(["rm", str(d)]), 1)
        self.assertTrue(d.exists())

    def test_mv_dry_run_keeps_both(self):
        src = Path(self.tmp) / "a.txt"
        src.write_text("x", encoding="utf-8")
        dst = Path(self.tmp) / "b.txt"
        self.assertEqual(minas.main(["mv", str(src), str(dst), "--dry-run"]), 0)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    def test_mv_real_moves(self):
        src = Path(self.tmp) / "a.txt"
        src.write_text("x", encoding="utf-8")
        dst = Path(self.tmp) / "b.txt"
        self.assertEqual(minas.main(["mv", str(src), str(dst)]), 0)
        self.assertFalse(src.exists())
        self.assertTrue(dst.exists())


class TestNetDecoding(unittest.TestCase):
    def test_decode_prefers_cp936_for_chinese_windows(self):
        raw = "命令成功完成".encode("cp936")
        self.assertEqual(minas._decode_net_bytes(raw), "命令成功完成")

    def test_decode_utf8_when_preferred_is_utf8(self):
        raw = "hello 世界".encode("utf-8")
        with mock.patch.object(minas.locale, "getpreferredencoding", return_value="utf-8"):
            self.assertEqual(minas._decode_net_bytes(raw), "hello 世界")

    def test_decode_empty(self):
        self.assertEqual(minas._decode_net_bytes(b""), "")


class TestResolvePassword(unittest.TestCase):
    def test_password_file_wins_over_interactive(self):
        pw_file = Path(tempfile.mkdtemp()) / "pw.txt"
        self.addCleanup(shutil.rmtree, pw_file.parent, True)
        pw_file.write_text("secret-from-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MINAS_PASSWORD_FILE": str(pw_file)}), mock.patch.object(
            sys, "stdin", mock.Mock(isatty=lambda: True)
        ), mock.patch.object(sys, "stdout", mock.Mock(isatty=lambda: True)):
            self.assertEqual(minas.resolve_password(), "secret-from-file")

    def test_interactive_star_when_no_file(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MINAS_")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            sys, "stdin", mock.Mock(isatty=lambda: True)
        ), mock.patch.object(sys, "stdout", mock.Mock(isatty=lambda: True)), mock.patch.object(
            minas, "IS_WINDOWS", True
        ):
            self.assertEqual(minas.resolve_password(), "*")


class TestMapRemoteNoPasswordInArgv(unittest.TestCase):
    def test_password_not_in_net_argv(self):
        seen: list[list[str]] = []

        def fake_run_net(args, timeout=30, capture=True):
            seen.append(list(args))
            return 0, "", ""

        def fake_wnet(target, user, password):
            return 0, ""

        with mock.patch.object(minas, "run_net", side_effect=fake_run_net), mock.patch.object(
            minas, "_wnet_add", side_effect=fake_wnet
        ), mock.patch.object(minas, "IS_WINDOWS", True):
            code, out, err = minas._map_remote("\\\\nas\\share", "u", "p@ss", False)
        self.assertEqual(code, 0)
        for args in seen:
            self.assertNotIn("p@ss", args)

    def test_interactive_uses_star_prompt(self):
        seen: list[list[str]] = []

        def fake_run_net(args, timeout=30, capture=True):
            seen.append(list(args))
            return 0, "", ""

        with mock.patch.object(minas, "run_net", side_effect=fake_run_net), mock.patch.object(
            minas, "IS_WINDOWS", True
        ):
            code, _, _ = minas._map_remote("\\\\nas\\share", "u", "*", True)
        self.assertEqual(code, 0)
        self.assertTrue(seen and seen[0][-1] == "*")


class TestCpForceAndDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.patches = [
            mock.patch.object(minas, "safe_remote", side_effect=lambda p: p),
            mock.patch.object(minas, "to_local", side_effect=lambda p: Path(p)),
            mock.patch.object(minas, "parse_unc", return_value=("h", "s", "")),
            mock.patch.dict(os.environ, {"MINAS_USER": ""}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_cp_force_overwrites_existing_dir(self):
        src = Path(self.tmp) / "src"
        src.mkdir()
        (src / "a.txt").write_text("new", encoding="utf-8")
        dst = Path(self.tmp) / "dst"
        dst.mkdir()
        (dst / "a.txt").write_text("old", encoding="utf-8")
        self.assertEqual(minas.main(["cp", str(src), str(dst), "--force"]), 0)
        self.assertEqual((dst / "a.txt").read_text(encoding="utf-8"), "new")

    def test_cp_dry_run_changes_nothing(self):
        src = Path(self.tmp) / "src"
        src.mkdir()
        (src / "a.txt").write_text("new", encoding="utf-8")
        dst = Path(self.tmp) / "dst"
        self.assertEqual(minas.main(["cp", str(src), str(dst), "--dry-run"]), 0)
        self.assertFalse(dst.exists())


class TestSharesChineseNoise(unittest.TestCase):
    def test_chinese_banner_words_filtered(self):
        sample = (
            "共享名     类型     用于    注释\r\n"
            "--------------------------------------\r\n"
            "public     Disk\r\n"
            "命令成功完成。\r\n"
        )
        with mock.patch.object(minas, "ensure_session"), mock.patch.object(
            minas, "run_net", return_value=(0, sample, "")
        ), mock.patch.object(minas, "IS_WINDOWS", True), mock.patch.dict(
            os.environ, {"MINAS_HOST": "nas"}
        ):
            code, names = minas.list_shares("nas")
        self.assertEqual(code, 0)
        self.assertEqual(names, ["public"])


if __name__ == "__main__":
    unittest.main()
