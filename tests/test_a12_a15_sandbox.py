"""A12–A15: sandboxed scorer isolation."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from turbine.sandbox import (
    format_isolation_report,
    probe_isolation,
    run_candidate,
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_dead(pid: int, limit_s: float = 2.0) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    return not _alive(pid)


class TestA12TimeoutAndReap(unittest.TestCase):
    def test_infinite_loop_times_out_and_kills_group(self) -> None:
        code = "\n".join(
            [
                "import os, time",
                "pid = os.fork()",
                "if pid == 0:",
                "    time.sleep(10**6)",
                "f = open('childpid','w',encoding='utf-8')",
                "f.write(str(pid))",
                "f.flush()",
                "os.fsync(f.fileno())",
                "f.close()",
                "while True:",
                "    pass",
            ]
        )
        t0 = time.monotonic()
        result = run_candidate(
            code,
            timeout=1.0,
            memory_bytes=1024 * 1024 * 1024,
            isolation_check=lambda: False,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(result.score, 0.0)
        self.assertIn("timeout", result.feedback.lower())
        self.assertLess(elapsed, 5.0)
        self.assertIsNotNone(result.workdir)
        child_file = Path(result.workdir) / "childpid"
        self.assertTrue(child_file.is_file(), "childpid must exist in workdir")
        child_pid = int(child_file.read_text(encoding="utf-8").strip())
        self.assertTrue(_wait_dead(child_pid), f"grandchild {child_pid} still alive")

    def test_normal_exit_reaps_forked_sleeper(self) -> None:
        code = "\n".join(
            [
                "import os, time",
                "pid = os.fork()",
                "if pid == 0:",
                "    time.sleep(10**6)",
                "f = open('childpid','w',encoding='utf-8')",
                "f.write(str(pid))",
                "f.flush()",
                "os.fsync(f.fileno())",
                "f.close()",
            ]
        )
        result = run_candidate(
            code, timeout=3.0, isolation_check=lambda: False
        )
        self.assertEqual(result.score, 1.0)
        child_file = Path(result.workdir) / "childpid"
        self.assertTrue(child_file.is_file())
        child_pid = int(child_file.read_text(encoding="utf-8").strip())
        self.assertTrue(_wait_dead(child_pid), f"sleeper {child_pid} still alive")

    def test_setsid_child_killed_on_timeout(self) -> None:
        code = "\n".join(
            [
                "import os, time",
                "pid = os.fork()",
                "if pid == 0:",
                "    os.setsid()",
                "    time.sleep(10**6)",
                "f = open('childpid','w',encoding='utf-8')",
                "f.write(str(pid))",
                "f.flush()",
                "os.fsync(f.fileno())",
                "f.close()",
                "while True:",
                "    pass",
            ]
        )
        result = run_candidate(
            code,
            timeout=1.0,
            memory_bytes=1024 * 1024 * 1024,
            isolation_check=lambda: False,
        )
        self.assertEqual(result.score, 0.0)
        self.assertIn("timeout", result.feedback.lower())
        child_file = Path(result.workdir) / "childpid"
        self.assertTrue(child_file.is_file())
        child_pid = int(child_file.read_text(encoding="utf-8").strip())
        self.assertTrue(
            _wait_dead(child_pid), f"setsid sleeper {child_pid} still alive"
        )


class TestA13EnvAndWrites(unittest.TestCase):
    def test_secrets_scrubbed_from_candidate_env(self) -> None:
        code = "\n".join(
            [
                "import os, json",
                "print(json.dumps(sorted(os.environ.keys())))",
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "TURBINE_TEST_API_KEY": "sk-test",
                "OPENAI_API_KEY": "sk-test-openai",
            },
            clear=False,
        ):
            result = run_candidate(code, timeout=2.0, isolation_check=lambda: False)
        self.assertNotIn("TURBINE_TEST_API_KEY", result.stdout)
        self.assertNotIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("sk-test", result.stdout)

    def test_parent_environ_not_readable_via_proc(self) -> None:
        code = "\n".join(
            [
                "import os",
                "ppid = os.getppid()",
                "paths = [",
                "    f'/proc/{ppid}/environ',",
                "    f'/proc/self/../{ppid}/environ',",
                "]",
                "chunks = []",
                "for p in paths:",
                "    try:",
                "        with open(p, 'rb') as f:",
                "            chunks.append(f.read().decode('utf-8', 'replace'))",
                "    except Exception as e:",
                "        chunks.append(f'ERR:{type(e).__name__}')",
                "print('---'.join(chunks))",
            ]
        )
        with mock.patch.dict(
            os.environ, {"TURBINE_TEST_API_KEY": "sk-parent-secret"}, clear=False
        ):
            result = run_candidate(code, timeout=2.0, isolation_check=lambda: False)
        self.assertNotIn("sk-parent-secret", result.stdout)
        self.assertNotIn("TURBINE_TEST_API_KEY", result.stdout)

    def test_outside_writes_invisible_on_host(self) -> None:
        probe = run_candidate(
            "print('ok')", timeout=2.0, isolation_check=lambda: True
        )
        report = format_isolation_report(probe)
        print(
            f"CI_ISOLATION network={probe.network_isolation} "
            f"filesystem={probe.filesystem_isolation} "
            f"errno={getattr(probe, 'isolation_detail', '')}",
            flush=True,
        )
        if not probe.filesystem_isolation:
            self.assertIn("filesystem_isolation: false", report)
            self.skipTest(
                "filesystem_isolation unavailable on this host; "
                f"report:\n{report} detail={getattr(probe, 'isolation_detail', '')!r}"
            )

        self.assertIn("filesystem_isolation: true", report)
        host_tmp = tempfile.gettempdir()
        vectors = {
            "pathlib": (
                "from pathlib import Path\n"
                "Path({path!r}).write_text('leaked', encoding='utf-8')\n"
                "print('wrote')\n"
            ),
            "posix_open": (
                "import posix\n"
                "fd = posix.open({path!r}, posix.O_WRONLY | posix.O_CREAT, 0o644)\n"
                "posix.write(fd, b'leaked')\n"
                "posix.close(fd)\n"
                "print('wrote')\n"
            ),
            "io_open": (
                "import _io\n"
                "f = _io.open({path!r}, 'w', encoding='utf-8')\n"
                "f.write('leaked')\n"
                "f.close()\n"
                "print('wrote')\n"
            ),
            "os_system": (
                "import os\n"
                "os.system('echo leaked > {path}')\n"
                "print('wrote')\n"
            ),
            "subprocess": (
                "import subprocess\n"
                "subprocess.run(['/bin/sh', '-c', 'echo leaked > {path}'], check=False)\n"
                "print('wrote')\n"
            ),
        }
        names = list(vectors.keys())
        for i in range(len(names)):
            name = names[i]
            with self.subTest(vector=name):
                host_path = (
                    Path(host_tmp) / f"turbine-a13-{name}-{uuid.uuid4().hex}.txt"
                )
                self.addCleanup(lambda p=host_path: p.unlink(missing_ok=True))
                if host_path.exists():
                    host_path.unlink()
                result = run_candidate(
                    vectors[name].format(path=str(host_path)),
                    timeout=3.0,
                    isolation_check=lambda: True,
                )
                self.assertTrue(result.filesystem_isolation)
                self.assertFalse(
                    host_path.is_file(),
                    f"{name} leaked to {host_path}",
                )


class TestA14CrashFeedback(unittest.TestCase):
    def test_raise_value_error(self) -> None:
        result = run_candidate(
            "raise ValueError('boom')", timeout=2.0, isolation_check=lambda: False
        )
        self.assertEqual(result.score, 0.0)
        self.assertIn("boom", result.feedback)

    def test_sys_exit_nonzero(self) -> None:
        result = run_candidate(
            "import sys; sys.exit(3)", timeout=2.0, isolation_check=lambda: False
        )
        self.assertEqual(result.score, 0.0)
        self.assertIn("exit 3", result.feedback)


class TestA15FlagsAndPlumbing(unittest.TestCase):
    def test_report_false_when_check_disables_attempt(self) -> None:
        result = run_candidate(
            "print('ok')", timeout=2.0, isolation_check=lambda: False
        )
        self.assertEqual(
            format_isolation_report(result),
            "network_isolation: false\nfilesystem_isolation: false",
        )

    def test_report_false_when_unshare_fails_despite_check(self) -> None:
        def boom_unshare(flags: int) -> None:
            raise OSError(1, "Operation not permitted")

        result = run_candidate(
            "print('ok')",
            timeout=2.0,
            isolation_check=lambda: True,
            unshare_fn=boom_unshare,
        )
        self.assertEqual(
            format_isolation_report(result),
            "network_isolation: false\nfilesystem_isolation: false",
        )

    def test_stubbed_true_branch_flag_plumbing(self) -> None:
        """True-branch plumbing must work even when os.unshare is absent."""
        result = run_candidate(
            "print('ok')",
            timeout=2.0,
            isolation_check=lambda: True,
            unshare_fn=lambda flags: None,
            write_maps_fn=lambda pid, uid, gid: True,
            mount_fn=lambda target: True,
        )
        self.assertEqual(result.score, 1.0)
        self.assertTrue(result.network_isolation)
        self.assertTrue(result.filesystem_isolation)
        self.assertEqual(
            format_isolation_report(result),
            "network_isolation: true\nfilesystem_isolation: true",
        )

    def test_fake_isolation_file_ignored(self) -> None:
        code = "\n".join(
            [
                "from pathlib import Path",
                "Path('.isolation').write_text('1 1\\n', encoding='utf-8')",
                "print('ok')",
            ]
        )
        result = run_candidate(
            code, timeout=2.0, isolation_check=lambda: False
        )
        self.assertEqual(result.score, 1.0)
        self.assertFalse(result.network_isolation)
        self.assertFalse(result.filesystem_isolation)

    def test_real_isolation_blocks_socket_when_available(self) -> None:
        net_ok, fs_ok, detail = probe_isolation()
        print(
            f"CI_PROBE network={net_ok} filesystem={fs_ok} detail={detail!r}",
            flush=True,
        )
        if not net_ok:
            result = run_candidate(
                "print('ok')", timeout=2.0, isolation_check=lambda: True
            )
            self.assertIn(
                "network_isolation: false", format_isolation_report(result)
            )
            self.skipTest(
                "network_isolation unavailable "
                f"(probe net={net_ok} fs={fs_ok} detail={detail!r})"
            )
        code = "\n".join(
            [
                "import socket",
                "s = socket.socket()",
                "s.settimeout(1.0)",
                "try:",
                "    s.connect(('1.1.1.1', 53))",
                "    print('CONNECTED')",
                "except Exception:",
                "    print('BLOCKED')",
                "finally:",
                "    s.close()",
            ]
        )
        result = run_candidate(code, timeout=3.0, isolation_check=lambda: True)
        self.assertTrue(result.network_isolation)
        self.assertIn("BLOCKED", result.stdout)
        self.assertNotIn("CONNECTED", result.stdout)

    def test_workdir_survives_tmpdir_mask(self) -> None:
        fake_tmp = tempfile.mkdtemp(prefix="turbine-fake-tmp-")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(fake_tmp, ignore_errors=True)
        )
        with mock.patch.dict(os.environ, {"TMPDIR": fake_tmp}, clear=False):
            # Force gettempdir to see TMPDIR.
            tempfile.tempdir = None
            result = run_candidate(
                "print('ok')",
                timeout=2.0,
                isolation_check=lambda: True,
                unshare_fn=lambda flags: None,
                write_maps_fn=lambda pid, uid, gid: True,
                mount_fn=lambda target: True,
            )
        self.assertEqual(result.score, 1.0)
        self.assertIsNotNone(result.workdir)
        self.assertFalse(
            str(result.workdir).startswith(fake_tmp + os.sep)
            or result.workdir == fake_tmp,
            f"workdir {result.workdir!r} is under TMPDIR {fake_tmp!r}",
        )


if __name__ == "__main__":
    unittest.main()
