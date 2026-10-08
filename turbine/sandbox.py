"""Sandboxed candidate runner: subprocess, scrubbed env, rlimits, timeout."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Allowlist only — never pass host secrets through.
_ENV_ALLOW = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "TZ",
        "USER",
        "LOGNAME",
    }
)

_HARNESS = textwrap.dedent(
    r"""
    import builtins, io, os, sys
    from pathlib import Path
    _CWD = Path.cwd().resolve()
    _open = builtins.open
    _io_open = io.open
    _os_open = os.open

    def _inside(path):
        try:
            p = Path(path).resolve()
        except Exception:
            return False
        return p == _CWD or _CWD in p.parents

    def _guard_open(file, mode='r', *a, **k):
        writing = any(c in str(mode) for c in 'wxa+')
        if writing and not _inside(file):
            raise PermissionError('outside sandbox cwd: %s' % (file,))
        return _io_open(file, mode, *a, **k)

    def os_open(path, flags, mode=0o777, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT):
            if not _inside(path):
                raise PermissionError('outside sandbox cwd: %s' % (path,))
        return _os_open(path, flags, mode, *a, **k)

    builtins.open = _guard_open
    io.open = _guard_open
    os.open = os_open
    # Candidate follows.
    """
)


@dataclass
class SandboxResult:
    score: float
    feedback: str
    network_isolation: bool
    stdout: str = ""
    stderr: str = ""
    workdir: str | None = None


NetworkCheck = Callable[[], bool]


def default_network_check() -> bool:
    """True when the OS can deny network for the child (best-effort)."""
    if not hasattr(os, "unshare"):
        return False
    # CLONE_NEWNET requires privileges on most hosts; probe in a short fork.
    CLONE_NEWNET = getattr(os, "CLONE_NEWNET", 0x40000000)
    try:
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.unshare(CLONE_NEWNET)
                os._exit(0)
            except Exception:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    except Exception:
        return False


def format_isolation_line(result: SandboxResult) -> str:
    return f"network_isolation: {'true' if result.network_isolation else 'false'}"


def _scrubbed_env(workdir: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env["HOME"] = workdir
    env["TMPDIR"] = workdir
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _preexec(memory_bytes: int, cpu_seconds: int, file_bytes: int, deny_net: bool):
    def _set() -> None:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        except Exception:
            pass
        if deny_net and hasattr(os, "unshare"):
            try:
                os.unshare(getattr(os, "CLONE_NEWNET", 0x40000000))
            except Exception:
                pass

    return _set


def run_candidate(
    code: str,
    *,
    timeout: float = 5.0,
    memory_bytes: int = 1024 * 1024 * 1024,
    cpu_seconds: int = 5,
    file_bytes: int = 8 * 1024 * 1024,
    network_check: NetworkCheck | None = None,
    keep_workdir: bool = True,
) -> SandboxResult:
    """Run candidate source in an isolated subprocess. Never raises to caller."""
    check = network_check if network_check is not None else default_network_check
    try:
        isolation = bool(check())
    except Exception:
        isolation = False

    workdir = tempfile.mkdtemp(prefix="turbine-sandbox-")
    script = Path(workdir) / "candidate.py"
    try:
        script.write_text(_HARNESS + "\n" + code + "\n", encoding="utf-8")
        cmd = [sys.executable, "-I", str(script)]
        preexec = None
        if os.name == "posix":
            preexec = _preexec(memory_bytes, cpu_seconds, file_bytes, isolation)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=_scrubbed_env(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=preexec,
            )
        except Exception as exc:
            return SandboxResult(
                0.0, f"spawn failed: {exc}", isolation, workdir=workdir
            )
        try:
            out_b, err_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out_b, err_b = proc.communicate(timeout=2.0)
            except Exception:
                out_b, err_b = b"", b""
            return SandboxResult(
                score=0.0,
                feedback=f"timeout after {timeout}s",
                network_isolation=isolation,
                stdout=out_b.decode("utf-8", "replace"),
                stderr=err_b.decode("utf-8", "replace"),
                workdir=workdir,
            )
        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            reason = err.strip() or out.strip() or f"exit {proc.returncode}"
            if str(proc.returncode) not in reason:
                reason = f"{reason} (exit {proc.returncode})"
            return SandboxResult(
                0.0, reason, isolation, stdout=out, stderr=err, workdir=workdir
            )
        return SandboxResult(
            1.0, "ok", isolation, stdout=out, stderr=err, workdir=workdir
        )
    except Exception as exc:
        return SandboxResult(0.0, f"sandbox error: {exc}", isolation, workdir=workdir)
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
