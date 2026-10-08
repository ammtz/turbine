"""Sandboxed candidate runner: namespaces, scrubbed env, rlimits, timeout."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import signal
import sys
import tempfile
import textwrap
import time
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

CLONE_NEWUSER = getattr(os, "CLONE_NEWUSER", 0x10000000)
CLONE_NEWNS = getattr(os, "CLONE_NEWNS", 0x00020000)
CLONE_NEWNET = getattr(os, "CLONE_NEWNET", 0x40000000)
MS_REC = 16384
MS_PRIVATE = 1 << 18

_HARNESS = textwrap.dedent(
    r"""
    import builtins, io, os, sys
    from pathlib import Path
    _CWD = Path.cwd().resolve()
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
    filesystem_isolation: bool = False
    stdout: str = ""
    stderr: str = ""
    workdir: str | None = None


IsolationCheck = Callable[[], bool]
UnshareFn = Callable[[int], None]


def default_isolation_check() -> bool:
    """True when this host should attempt userns isolation."""
    return os.name == "posix" and hasattr(os, "unshare")


default_network_check = default_isolation_check


def format_isolation_report(result: SandboxResult) -> str:
    net = "true" if result.network_isolation else "false"
    fs = "true" if result.filesystem_isolation else "false"
    return f"network_isolation: {net}\nfilesystem_isolation: {fs}"


def format_isolation_line(result: SandboxResult) -> str:
    return format_isolation_report(result)


def _scrubbed_env(workdir: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env["HOME"] = workdir
    env["TMPDIR"] = workdir
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _workdir_root() -> str:
    for candidate in ("/var/tmp", str(Path.cwd()), tempfile.gettempdir()):
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
            if os.access(candidate, os.W_OK):
                return candidate
        except Exception:
            continue
    return tempfile.gettempdir()


def _mount_tmpfs(target: str) -> bool:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        return False
    return libc.mount(b"tmpfs", target.encode(), b"tmpfs", 0, b"size=64m") == 0


def _write_id_maps(pid: int, uid: int, gid: int) -> bool:
    try:
        with open(f"/proc/{pid}/setgroups", "w", encoding="utf-8") as f:
            f.write("deny")
        with open(f"/proc/{pid}/uid_map", "w", encoding="utf-8") as f:
            f.write(f"0 {uid} 1\n")
        with open(f"/proc/{pid}/gid_map", "w", encoding="utf-8") as f:
            f.write(f"0 {gid} 1\n")
        return True
    except Exception:
        return False


def _set_rlimits(memory_bytes: int, cpu_seconds: int, file_bytes: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    except Exception:
        pass


def probe_isolation(
    *,
    unshare_fn: UnshareFn | None = None,
    host_tmpdir: str | None = None,
) -> tuple[bool, bool]:
    """Fork-probe whether isolation can be applied. Never raises."""
    if os.name != "posix" or not hasattr(os, "unshare"):
        return False, False
    tmp = host_tmpdir or tempfile.gettempdir()
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        pid = os.fork()
    except Exception:
        for fd in (ready_r, ready_w, go_r, go_w, out_r, out_w):
            try:
                os.close(fd)
            except Exception:
                pass
        return False, False

    if pid == 0:
        os.close(ready_r)
        os.close(go_w)
        os.close(out_r)
        try:
            unshare = unshare_fn or os.unshare
            unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET)
            os.write(ready_w, b"1")
            os.close(ready_w)
            if os.read(go_r, 1) != b"1":
                os.write(out_w, b"0 0\n")
                os._exit(1)
            os.close(go_r)
            fs_ok = _mount_tmpfs(tmp)
            os.write(out_w, f"1 {int(fs_ok)}\n".encode())
            os.close(out_w)
            os._exit(0)
        except Exception:
            try:
                os.close(ready_w)
            except Exception:
                pass
            try:
                os.write(out_w, b"0 0\n")
                os.close(out_w)
            except Exception:
                pass
            os._exit(1)

    os.close(ready_w)
    os.close(go_r)
    os.close(out_w)
    net_ok, fs_ok = False, False
    try:
        if os.read(ready_r, 1) == b"1":
            if _write_id_maps(pid, os.getuid(), os.getgid()):
                os.write(go_w, b"1")
            else:
                os.write(go_w, b"0")
        os.close(go_w)
        raw = os.read(out_r, 64).decode("utf-8", "replace").strip().split()
        if len(raw) == 2:
            net_ok, fs_ok = raw[0] == "1", raw[1] == "1"
    except Exception:
        net_ok, fs_ok = False, False
    finally:
        for fd in (ready_r, out_r):
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
    return net_ok, fs_ok


def run_candidate(
    code: str,
    *,
    timeout: float = 5.0,
    memory_bytes: int = 1024 * 1024 * 1024,
    cpu_seconds: int = 5,
    file_bytes: int = 8 * 1024 * 1024,
    isolation_check: IsolationCheck | None = None,
    network_check: IsolationCheck | None = None,
    unshare_fn: UnshareFn | None = None,
    keep_workdir: bool = True,
) -> SandboxResult:
    """Run candidate source in an isolated subprocess. Never raises to caller."""
    check = isolation_check or network_check or default_isolation_check
    try:
        attempt = bool(check())
    except Exception:
        attempt = False

    host_tmpdir = tempfile.gettempdir()
    workdir = tempfile.mkdtemp(prefix="turbine-sandbox-", dir=_workdir_root())
    script = Path(workdir) / "candidate.py"
    stdout_path = Path(workdir) / ".stdout"
    stderr_path = Path(workdir) / ".stderr"
    iso_path = Path(workdir) / ".isolation"
    net_flag = False
    fs_flag = False

    try:
        script.write_text(_HARNESS + "\n" + code + "\n", encoding="utf-8")
        if os.name != "posix":
            return SandboxResult(
                0.0,
                "sandbox requires POSIX",
                False,
                False,
                workdir=workdir,
            )

        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        try:
            pid = os.fork()
        except Exception as exc:
            os.close(ready_r)
            os.close(ready_w)
            os.close(go_r)
            os.close(go_w)
            return SandboxResult(
                0.0, f"spawn failed: {exc}", False, False, workdir=workdir
            )

        if pid == 0:
            # Child: optional isolation, then exec candidate.
            os.close(ready_r)
            os.close(go_w)
            try:
                os.setsid()
            except Exception:
                pass
            got_net = False
            got_fs = False
            try:
                if attempt:
                    unshare = unshare_fn or os.unshare
                    unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET)
                    os.write(ready_w, b"1")
                    os.close(ready_w)
                    token = os.read(go_r, 1)
                    os.close(go_r)
                    if token == b"1":
                        got_net = True
                        got_fs = _mount_tmpfs(host_tmpdir)
                    else:
                        got_net, got_fs = False, False
                else:
                    os.close(ready_w)
                    os.close(go_r)
            except Exception:
                try:
                    os.close(ready_w)
                except Exception:
                    pass
                try:
                    os.close(go_r)
                except Exception:
                    pass
                got_net, got_fs = False, False

            try:
                iso_path.write_text(
                    f"{int(got_net)} {int(got_fs)}\n", encoding="utf-8"
                )
            except Exception:
                pass

            _set_rlimits(memory_bytes, cpu_seconds, file_bytes)
            try:
                os.chdir(workdir)
                out_fd = os.open(
                    str(stdout_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
                )
                err_fd = os.open(
                    str(stderr_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
                )
                os.dup2(out_fd, 1)
                os.dup2(err_fd, 2)
                os.close(out_fd)
                os.close(err_fd)
                env = _scrubbed_env(workdir)
                os.execve(
                    sys.executable,
                    [sys.executable, "-I", str(script)],
                    env,
                )
            except Exception as exc:
                try:
                    sys.stderr.write(f"exec failed: {exc}\n")
                except Exception:
                    pass
                os._exit(127)

        # Parent
        os.close(ready_w)
        os.close(go_r)
        try:
            if attempt:
                ready = os.read(ready_r, 1)
                if ready == b"1" and _write_id_maps(
                    pid, os.getuid(), os.getgid()
                ):
                    os.write(go_w, b"1")
                else:
                    os.write(go_w, b"0")
            else:
                # Child closed ready_w without writing when attempt is false.
                pass
        except Exception:
            try:
                os.write(go_w, b"0")
            except Exception:
                pass
        finally:
            try:
                os.close(ready_r)
            except Exception:
                pass
            try:
                os.close(go_w)
            except Exception:
                pass

        timed_out = False
        status = 0
        deadline = time.monotonic() + timeout
        while True:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = pid, 0
                break
            if waited == pid:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                try:
                    _, status = os.waitpid(pid, 0)
                except Exception:
                    pass
                break
            time.sleep(0.02)

        if iso_path.is_file():
            parts = iso_path.read_text(encoding="utf-8").strip().split()
            if len(parts) == 2:
                net_flag, fs_flag = parts[0] == "1", parts[1] == "1"

        out = (
            stdout_path.read_text(encoding="utf-8", errors="replace")
            if stdout_path.is_file()
            else ""
        )
        err = (
            stderr_path.read_text(encoding="utf-8", errors="replace")
            if stderr_path.is_file()
            else ""
        )

        if timed_out:
            return SandboxResult(
                score=0.0,
                feedback=f"timeout after {timeout}s",
                network_isolation=net_flag,
                filesystem_isolation=fs_flag,
                stdout=out,
                stderr=err,
                workdir=workdir,
            )

        code_rc = 0
        if os.WIFEXITED(status):
            code_rc = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            code_rc = -os.WTERMSIG(status)

        if code_rc != 0:
            detail = err.strip() or out.strip() or "error"
            return SandboxResult(
                0.0,
                f"exit {code_rc}: {detail}",
                net_flag,
                fs_flag,
                stdout=out,
                stderr=err,
                workdir=workdir,
            )
        return SandboxResult(
            1.0,
            "ok",
            net_flag,
            fs_flag,
            stdout=out,
            stderr=err,
            workdir=workdir,
        )
    except Exception as exc:
        return SandboxResult(
            0.0,
            f"sandbox error: {exc}",
            net_flag,
            fs_flag,
            workdir=workdir,
        )
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
