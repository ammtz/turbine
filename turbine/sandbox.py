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
CLONE_NEWPID = getattr(os, "CLONE_NEWPID", 0x20000000)
MS_REC = 16384
MS_PRIVATE = 1 << 18
PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3
PR_SET_CHILD_SUBREAPER = 36

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
    isolation_detail: str = ""


IsolationCheck = Callable[[], bool]
UnshareFn = Callable[[int], None]
WriteMapsFn = Callable[[int, int, int], bool]
MountFn = Callable[[str], bool]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "c", use_errno=True)


def do_unshare(flags: int) -> None:
    """Unshare via os.unshare (3.12+) or libc.unshare (3.11)."""
    if hasattr(os, "unshare"):
        os.unshare(flags)
        return
    libc = _libc()
    if libc.unshare(flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def default_isolation_check() -> bool:
    return os.name == "posix"


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


def _is_under(path: str, root: str) -> bool:
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
        return p == r or r in p.parents
    except Exception:
        return False


def _workdir_root(host_tmpdir: str) -> str:
    for candidate in ("/var/tmp", str(Path.cwd())):
        try:
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                if not _is_under(candidate, host_tmpdir):
                    return candidate
        except Exception:
            continue
    return "/var/tmp"


def _mount_tmpfs(target: str) -> bool:
    libc = _libc()
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


def _prctl_dumpable(value: int) -> int | None:
    try:
        libc = _libc()
        prev = libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)
        if libc.prctl(PR_SET_DUMPABLE, value, 0, 0, 0) != 0:
            return None
        return int(prev)
    except Exception:
        return None


def _prctl_child_subreaper(value: int) -> bool:
    try:
        return _libc().prctl(PR_SET_CHILD_SUBREAPER, value, 0, 0, 0) == 0
    except Exception:
        return False


def _collect_descendants(root_pid: int, limit: int = 256) -> list[int]:
    children: dict[int, list[int]] = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="utf-8") as f:
                    stat = f.read()
                rparen = stat.rfind(")")
                if rparen < 0:
                    continue
                parts = stat[rparen + 2 :].split()
                if len(parts) < 2:
                    continue
                children.setdefault(int(parts[1]), []).append(int(name))
            except Exception:
                continue
    except Exception:
        return []
    out: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack and len(out) < limit:
        pid = stack.pop()
        if pid in out or pid == root_pid:
            continue
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _reap_tree(root_pid: int) -> None:
    try:
        os.killpg(root_pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(root_pid, signal.SIGKILL)
        except Exception:
            pass
    for _ in range(8):
        descendants = _collect_descendants(root_pid, limit=256)
        if not descendants:
            break
        for dpid in descendants:
            try:
                os.kill(dpid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(0.02)


def probe_isolation(
    *,
    unshare_fn: UnshareFn | None = None,
    host_tmpdir: str | None = None,
) -> tuple[bool, bool, str]:
    if os.name != "posix":
        return False, False, "non-posix"
    tmp = host_tmpdir or tempfile.gettempdir()
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        pid = os.fork()
    except Exception as exc:
        for fd in (ready_r, ready_w, go_r, go_w, out_r, out_w):
            try:
                os.close(fd)
            except Exception:
                pass
        return False, False, f"fork:{exc}"

    if pid == 0:
        os.close(ready_r)
        os.close(go_w)
        os.close(out_r)
        try:
            unshare = unshare_fn or do_unshare
            unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID)
            os.write(ready_w, b"1")
            os.close(ready_w)
            if os.read(go_r, 1) != b"1":
                os.write(out_w, b"0 0 maps_failed\n")
                os._exit(1)
            os.close(go_r)
            pid2 = os.fork()
            if pid2 != 0:
                os.waitpid(pid2, 0)
                os._exit(0)
            fs_ok = _mount_tmpfs(tmp)
            os.write(out_w, f"1 {int(fs_ok)} ok\n".encode())
            os.close(out_w)
            os._exit(0)
        except OSError as exc:
            try:
                os.close(ready_w)
            except Exception:
                pass
            try:
                os.write(out_w, f"0 0 errno={exc.errno}\n".encode())
                os.close(out_w)
            except Exception:
                pass
            os._exit(1)
        except Exception as exc:
            try:
                os.close(ready_w)
            except Exception:
                pass
            try:
                os.write(out_w, f"0 0 err={type(exc).__name__}\n".encode())
                os.close(out_w)
            except Exception:
                pass
            os._exit(1)

    os.close(ready_w)
    os.close(go_r)
    os.close(out_w)
    net_ok, fs_ok, detail = False, False, ""
    try:
        if os.read(ready_r, 1) == b"1":
            if _write_id_maps(pid, os.getuid(), os.getgid()):
                os.write(go_w, b"1")
            else:
                os.write(go_w, b"0")
                detail = "uid_map_eperm"
        else:
            detail = "unshare_no_ready"
        os.close(go_w)
        raw = os.read(out_r, 128).decode("utf-8", "replace").strip().split()
        if len(raw) >= 2:
            net_ok, fs_ok = raw[0] == "1", raw[1] == "1"
            if len(raw) >= 3:
                detail = raw[2]
    except Exception as exc:
        net_ok, fs_ok, detail = False, False, f"probe:{exc}"
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
    return net_ok, fs_ok, detail


def _exec_candidate(
    workdir: str,
    script: Path,
    stdout_path: Path,
    stderr_path: Path,
    memory_bytes: int,
    cpu_seconds: int,
    file_bytes: int,
) -> None:
    _set_rlimits(memory_bytes, cpu_seconds, file_bytes)
    os.chdir(workdir)
    out_fd = os.open(str(stdout_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    err_fd = os.open(str(stderr_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(out_fd, 1)
    os.dup2(err_fd, 2)
    os.close(out_fd)
    os.close(err_fd)
    os.execve(
        sys.executable,
        [sys.executable, "-I", str(script)],
        _scrubbed_env(workdir),
    )


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
    write_maps_fn: WriteMapsFn | None = None,
    mount_fn: MountFn | None = None,
    keep_workdir: bool = True,
) -> SandboxResult:
    """Run candidate source in an isolated subprocess. Never raises to caller."""
    check = isolation_check or network_check or default_isolation_check
    try:
        attempt = bool(check())
    except Exception:
        attempt = False

    host_tmpdir = tempfile.gettempdir()
    workdir = tempfile.mkdtemp(
        prefix="turbine-sandbox-", dir=_workdir_root(host_tmpdir)
    )
    script = Path(workdir) / "candidate.py"
    stdout_path = Path(workdir) / ".stdout"
    stderr_path = Path(workdir) / ".stderr"
    net_flag = False
    fs_flag = False
    detail = ""

    try:
        script.write_text(_HARNESS + "\n" + code + "\n", encoding="utf-8")
        if os.name != "posix":
            return SandboxResult(
                0.0, "sandbox requires POSIX", False, False, workdir=workdir
            )

        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        flag_r, flag_w = os.pipe()
        try:
            pid = os.fork()
        except Exception as exc:
            for fd in (ready_r, ready_w, go_r, go_w, flag_r, flag_w):
                try:
                    os.close(fd)
                except Exception:
                    pass
            return SandboxResult(
                0.0, f"spawn failed: {exc}", False, False, workdir=workdir
            )

        if pid == 0:
            os.close(ready_r)
            os.close(go_w)
            os.close(flag_r)
            try:
                os.setsid()
            except Exception:
                pass

            got_net = False
            got_fs = False
            child_detail = "ok"
            try:
                if attempt:
                    unshare = unshare_fn or do_unshare
                    unshare(
                        CLONE_NEWUSER
                        | CLONE_NEWNS
                        | CLONE_NEWNET
                        | CLONE_NEWPID
                    )
                    os.write(ready_w, b"1")
                    os.close(ready_w)
                    token = os.read(go_r, 1)
                    os.close(go_r)
                    if token == b"1":
                        got_net = True
                        # Mount happens after the NEWPID fork (see below).
                    else:
                        child_detail = "maps_failed"
                else:
                    os.close(ready_w)
                    os.close(go_r)
                    child_detail = "disabled"
            except OSError as exc:
                child_detail = f"errno={exc.errno}"
                for fd in (ready_w, go_r):
                    try:
                        os.close(fd)
                    except Exception:
                        pass
            except Exception as exc:
                child_detail = type(exc).__name__
                for fd in (ready_w, go_r):
                    try:
                        os.close(fd)
                    except Exception:
                        pass

            # NEWPID: fork before mount (mount-then-fork hits ENOMEM on some kernels).
            # Mount in the intermediate so the shared mount ns is ready, then release
            # the candidate (PID 1) to exec.
            hold_r, hold_w = os.pipe()
            try:
                pid1 = os.fork()
            except Exception:
                os._exit(127)
            if pid1 != 0:
                os.close(hold_r)
                # Candidate's ppid is this intermediate process; scrub dumpable
                # before it can read /proc/<ppid>/environ.
                _prctl_dumpable(0)
                # Orphans of the candidate reparent here instead of init, so we
                # can reap them without killing recycled host PIDs.
                _prctl_child_subreaper(1)
                if child_detail == "ok" and got_net:
                    mounter = mount_fn or _mount_tmpfs
                    got_fs = bool(mounter(host_tmpdir))
                try:
                    os.write(
                        flag_w,
                        f"{int(got_net)} {int(got_fs)} {child_detail}\n".encode(),
                    )
                    os.close(flag_w)
                except Exception:
                    try:
                        os.close(flag_w)
                    except Exception:
                        pass
                try:
                    os.write(hold_w, b"1")
                    os.close(hold_w)
                except Exception:
                    pass
                try:
                    _, st = os.waitpid(pid1, 0)
                except Exception:
                    os._exit(1)
                # Reap any children reparented to us as subreaper.
                for _ in range(32):
                    orphans = _collect_descendants(os.getpid(), limit=256)
                    if not orphans:
                        try:
                            wpid, _ = os.waitpid(-1, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if wpid == 0:
                            break
                        continue
                    for dpid in orphans:
                        try:
                            os.kill(dpid, signal.SIGKILL)
                        except Exception:
                            pass
                    try:
                        os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        break
                    time.sleep(0.02)
                if os.WIFEXITED(st):
                    os._exit(os.WEXITSTATUS(st))
                if os.WIFSIGNALED(st):
                    os._exit(128 + os.WTERMSIG(st))
                os._exit(1)

            os.close(hold_w)
            try:
                os.close(flag_w)
            except Exception:
                pass
            try:
                os.read(hold_r, 1)
            except Exception:
                pass
            try:
                os.close(hold_r)
            except Exception:
                pass
            try:
                _exec_candidate(
                    workdir,
                    script,
                    stdout_path,
                    stderr_path,
                    memory_bytes,
                    cpu_seconds,
                    file_bytes,
                )
            except Exception as exc:
                try:
                    sys.stderr.write(f"exec failed: {exc}\n")
                except Exception:
                    pass
                os._exit(127)

        # Parent: become subreaper so orphans of a killed intermediate reparent
        # here and can be reaped by current children, not by stale PIDs.
        _prctl_child_subreaper(1)
        os.close(ready_w)
        os.close(go_r)
        os.close(flag_w)
        maps = write_maps_fn or _write_id_maps
        try:
            if attempt:
                ready = os.read(ready_r, 1)
                # uid_map writes require this process to remain dumpable.
                # Environ scrub for the candidate is done in the intermediate
                # (pid1 != 0) before the candidate runs.
                mapped = ready == b"1" and maps(pid, os.getuid(), os.getgid())
                if mapped:
                    os.write(go_w, b"1")
                else:
                    os.write(go_w, b"0")
                    if ready == b"1":
                        detail = "uid_map_failed"
        except Exception as exc:
            detail = f"parent:{exc}"
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

        try:
            raw = os.read(flag_r, 128).decode("utf-8", "replace").strip().split()
            if len(raw) >= 2:
                net_flag, fs_flag = raw[0] == "1", raw[1] == "1"
                if len(raw) >= 3:
                    detail = raw[2]
        except Exception:
            pass
        finally:
            try:
                os.close(flag_r)
            except Exception:
                pass

        timed_out = False
        exited = False
        status = 0
        deadline = time.monotonic() + timeout
        while True:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = pid, 0
                exited = True
                break
            if waited == pid:
                exited = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)

        # Kill the sandbox leader/group; subreaper then lets us reap orphans
        # that were live children of this process (no stale-PID kill list).
        _reap_tree(pid)
        for _ in range(32):
            orphans = [
                dpid
                for dpid in _collect_descendants(os.getpid(), limit=256)
                if dpid != pid
            ]
            if not orphans:
                try:
                    wpid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if wpid == 0:
                    break
                continue
            for dpid in orphans:
                try:
                    os.kill(dpid, signal.SIGKILL)
                except Exception:
                    pass
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            time.sleep(0.02)
        if not exited:
            try:
                _, status = os.waitpid(pid, 0)
            except Exception:
                pass

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
                isolation_detail=detail,
            )

        code_rc = 0
        if os.WIFEXITED(status):
            code_rc = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            code_rc = -os.WTERMSIG(status)

        if code_rc != 0:
            return SandboxResult(
                0.0,
                f"exit {code_rc}: {err.strip() or out.strip() or 'error'}",
                net_flag,
                fs_flag,
                stdout=out,
                stderr=err,
                workdir=workdir,
                isolation_detail=detail,
            )
        return SandboxResult(
            1.0,
            "ok",
            net_flag,
            fs_flag,
            stdout=out,
            stderr=err,
            workdir=workdir,
            isolation_detail=detail,
        )
    except Exception as exc:
        return SandboxResult(
            0.0,
            f"sandbox error: {exc}",
            net_flag,
            fs_flag,
            workdir=workdir,
            isolation_detail=detail,
        )
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
