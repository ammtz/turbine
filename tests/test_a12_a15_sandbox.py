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


class TestA12Timeout(unittest.TestCase):
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
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(_alive(child_pid), f"grandchild {child_pid} still alive")


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
            self.assertEqual(os.environ.get("TURBINE_TEST_API_KEY"), "sk-test")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-test-openai")
            result = run_candidate(code, timeout=2.0, isolation_check=lambda: False)
        self.assertNotIn("TURBINE_TEST_API_KEY", result.stdout)
        self.assertNotIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("sk-test", result.stdout)
        self.assertNotIn("sk-test", result.feedback)

    def test_outside_writes_invisible_on_host(self) -> None:
        probe = run_candidate(
            "print('ok')", timeout=2.0, isolation_check=lambda: True
        )
        report = format_isolation_report(probe)
        print(
            f"CI_ISOLATION network={probe.network_isolation} "
            f"filesystem={probe.filesystem_isolation}",
            flush=True,
        )
        if not probe.filesystem_isolation:
            self.assertIn("filesystem_isolation: false", report)
            self.skipTest(
                "filesystem_isolation unavailable on this host "
                "(unprivileged userns/mount not usable); "
                f"report:\n{report}"
            )

        self.assertIn("filesystem_isolation: true", report)
        host_tmp = tempfile.gettempdir()
        vectors = {
            "pathlib": "\n".join(
                [
                    "from pathlib import Path",
                    "Path({path!r}).write_text('leaked', encoding='utf-8')",
                    "print('wrote')",
                ]
            ),
            "posix_open": "\n".join(
                [
                    "import posix",
                    "fd = posix.open({path!r}, posix.O_WRONLY | posix.O_CREAT, 0o644)",
                    "posix.write(fd, b'leaked')",
                    "posix.close(fd)",
                    "print('wrote')",
                ]
            ),
            "io_open": "\n".join(
                [
                    "import _io",
                    "f = _io.open({path!r}, 'w', encoding='utf-8')",
                    "f.write('leaked')",
                    "f.close()",
                    "print('wrote')",
                ]
            ),
            "os_system": "\n".join(
                [
                    "import os",
                    "os.system('echo leaked > {path}')",
                    "print('wrote')",
                ]
            ),
            "subprocess": "\n".join(
                [
                    "import subprocess",
                    "subprocess.run(['/bin/sh', '-c', 'echo leaked > {path}'], check=False)",
                    "print('wrote')",
                ]
            ),
        }
        names = list(vectors.keys())
        for i in range(len(names)):
            name = names[i]
            with self.subTest(vector=name):
                host_path = Path(host_tmp) / f"turbine-a13-{name}-{uuid.uuid4().hex}.txt"
                self.addCleanup(lambda p=host_path: p.unlink(missing_ok=True))
                if host_path.exists():
                    host_path.unlink()
                code = vectors[name].format(path=str(host_path))
                result = run_candidate(
                    code, timeout=3.0, isolation_check=lambda: True
                )
                self.assertTrue(
                    result.filesystem_isolation,
                    f"expected filesystem_isolation for {name}: "
                    f"{format_isolation_report(result)}",
                )
                self.assertFalse(
                    host_path.is_file(),
                    f"{name} leaked to host path {host_path} "
                    f"score={result.score} feedback={result.feedback!r}",
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


class TestA15NetworkIsolationReport(unittest.TestCase):
    def test_report_false_when_check_disables_attempt(self) -> None:
        result = run_candidate(
            "print('ok')", timeout=2.0, isolation_check=lambda: False
        )
        self.assertEqual(
            format_isolation_report(result),
            "network_isolation: false\nfilesystem_isolation: false",
        )
        self.assertFalse(result.network_isolation)
        self.assertFalse(result.filesystem_isolation)

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

    def test_real_isolation_blocks_socket_when_available(self) -> None:
        net_ok, fs_ok = probe_isolation()
        print(f"CI_PROBE network={net_ok} filesystem={fs_ok}", flush=True)
        if not net_ok:
            result = run_candidate(
                "print('ok')", timeout=2.0, isolation_check=lambda: True
            )
            self.assertIn("network_isolation: false", format_isolation_report(result))
            self.skipTest(
                "network_isolation unavailable on this host "
                f"(probe net={net_ok} fs={fs_ok})"
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
        self.assertEqual(
            format_isolation_report(result).splitlines()[0],
            "network_isolation: true",
        )
        self.assertIn("BLOCKED", result.stdout)
        self.assertNotIn("CONNECTED", result.stdout)


if __name__ == "__main__":
    unittest.main()
