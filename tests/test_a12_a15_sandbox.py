"""A12–A15: sandboxed scorer isolation."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from turbine.sandbox import run_candidate, format_isolation_line


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
            code, timeout=1.0, memory_bytes=1024 * 1024 * 1024, network_check=lambda: False
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(result.score, 0.0)
        self.assertIn("timeout", result.feedback.lower())
        self.assertLess(elapsed, 5.0)
        # Grandchild must be gone (process-group kill).
        child_file = Path(result.workdir) / "childpid" if result.workdir else None
        if child_file is not None and child_file.is_file():
            child_pid = int(child_file.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and _alive(child_pid):
                time.sleep(0.05)
            self.assertFalse(_alive(child_pid), f"grandchild {child_pid} still alive")
        else:
            # workdir may be cleaned; still require timeout outcome above
            self.assertIn("timeout", result.feedback.lower())


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
            result = run_candidate(code, timeout=2.0)
        self.assertNotIn("TURBINE_TEST_API_KEY", result.stdout)
        self.assertNotIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("sk-test", result.stdout)
        self.assertNotIn("sk-test", result.feedback)

    def test_outside_write_not_visible_or_fails(self) -> None:
        host_path = Path(tempfile.gettempdir()) / f"turbine-a13-{uuid.uuid4().hex}.txt"
        self.addCleanup(lambda: host_path.unlink(missing_ok=True))
        if host_path.exists():
            host_path.unlink()
        code = "\n".join(
            [
                "from pathlib import Path",
                f"p = Path({str(host_path)!r})",
                "p.write_text('leaked', encoding='utf-8')",
                "print('wrote')",
            ]
        )
        result = run_candidate(code, timeout=2.0)
        leaked = host_path.is_file()
        failed = result.score == 0.0 or "fail" in result.feedback.lower() or "permission" in result.feedback.lower() or "outside" in result.feedback.lower()
        self.assertTrue(
            (not leaked) or failed,
            f"leak={leaked} score={result.score} feedback={result.feedback!r}",
        )
        if leaked:
            self.assertEqual(result.score, 0.0)


class TestA14CrashFeedback(unittest.TestCase):
    def test_raise_value_error(self) -> None:
        result = run_candidate("raise ValueError('boom')", timeout=2.0)
        self.assertEqual(result.score, 0.0)
        self.assertIn("boom", result.feedback)

    def test_sys_exit_nonzero(self) -> None:
        result = run_candidate("import sys; sys.exit(3)", timeout=2.0)
        self.assertEqual(result.score, 0.0)
        self.assertIn("3", result.feedback)


class TestA15NetworkIsolationReport(unittest.TestCase):
    def test_report_true_when_check_says_true(self) -> None:
        result = run_candidate("print('ok')", timeout=2.0, network_check=lambda: True)
        self.assertEqual(format_isolation_line(result), "network_isolation: true")
        self.assertTrue(result.network_isolation)

    def test_report_false_when_check_says_false(self) -> None:
        result = run_candidate("print('ok')", timeout=2.0, network_check=lambda: False)
        self.assertEqual(format_isolation_line(result), "network_isolation: false")
        self.assertFalse(result.network_isolation)


if __name__ == "__main__":
    unittest.main()
