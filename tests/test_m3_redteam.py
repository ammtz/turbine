"""Bounded sandbox attacks; failed assertions are intentionally retained findings.

Only local sockets and a reserved example.com DNS lookup; no real secrets.
Fork/thread stress is capped rather
than exhausting the host. Cleanup happens even when a containment assertion fails.
The host-secret and timeout-escape tests exercise the supported no-namespace
fallback explicitly; other tests attempt real namespace isolation.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from turbine.sandbox import format_isolation_report, run_candidate


class TestM3Redteam(unittest.TestCase):
    def candidate(self, code, **kwargs):
        start = time.monotonic()
        result = run_candidate(textwrap.dedent(code), timeout=1.0, **kwargs)
        print(f'REDTEAM net={result.network_isolation} fs={result.filesystem_isolation} '
              f'detail={result.isolation_detail!r}', flush=True)
        self.addCleanup(shutil.rmtree, result.workdir, True)
        self.addCleanup(self.kill_remaining, result.workdir)
        self.assertLess(time.monotonic() - start, 5.0)
        return result

    @staticmethod
    def live_in(workdir):
        """Identify only our candidates by their unique cwd; zombies aren't live."""
        found = set()
        # Cross-user-namespace /proc cwd reads can be denied. Candidates also
        # record host PID + start time, avoiding namespace PIDs and PID reuse.
        record = Path(workdir) / 'processes'
        if record.exists():
            for line in record.read_text().splitlines():
                try:
                    pid, birth = line.split()
                    fields = (Path('/proc') / pid / 'stat').read_text().rsplit(')', 1)[1].split()
                    if fields[19] == birth and fields[0] != 'Z':
                        found.add(int(pid))
                except (OSError, ValueError):
                    pass
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.readlink(entry / 'cwd') == workdir:
                    state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                    if state != 'Z':
                        found.add(int(entry.name))
            except (OSError, ValueError):
                pass
        return sorted(found)

    @classmethod
    def kill_remaining(cls, workdir):
        for pid in cls.live_in(workdir):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def assert_executed(self, result, marker='ATTEMPTED'):
        self.assertIn(marker, result.stdout, result.feedback)

    def test_secrets_os_environ_and_proc_self(self):
        marker = 'synthetic-' + uuid.uuid4().hex
        with patch.dict(os.environ, {'REDTEAM_SECRET': marker}):
            result = self.candidate(f"""
                import os, json
                marker = {marker!r}.encode()
                print('ATTEMPTED')
                print(json.dumps({{
                    'env': 'REDTEAM_SECRET' in os.environ,
                    'self': marker in open('/proc/self/environ', 'rb').read(),
                }}))
            """)
        self.assert_executed(result)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]),
                         {'env': False, 'self': False})

    def test_secrets_proc_init_parent_and_synthetic_host(self):
        marker = 'synthetic-' + uuid.uuid4().hex
        fixture = subprocess.Popen(
            [sys.executable, '-I', '-c', 'import time; time.sleep(8)'],
            env={'REDTEAM_SECRET': marker},
        )
        self.addCleanup(fixture.wait, 2)
        self.addCleanup(fixture.kill)
        result = self.candidate(f"""
            import os, json
            paths = ['/proc/1/environ', f'/proc/{{os.getppid()}}/environ',
                     '/proc/{os.getpid()}/environ', '/proc/{fixture.pid}/environ']
            found = {{}}
            for path in paths:
                try:
                    data = open(path, 'rb').read()
                    found[path] = {{'readable': True,
                                   'synthetic_secret': {marker!r}.encode() in data}}
                except OSError:
                    found[path] = {{'readable': False, 'synthetic_secret': False}}
            print('ATTEMPTED')
            print(json.dumps(found))
        """, isolation_check=lambda: False)
        self.assert_executed(result)
        observations = json.loads(result.stdout.splitlines()[-1])
        self.assertFalse(any(v['synthetic_secret'] for v in observations.values()),
                         f'synthetic host secret read: {observations}')
        self.assertFalse(any(v['readable'] for v in observations.values()),
                         f'host process environments readable: {observations}')

    def write_attack(self, root, vector):
        target = Path(root) / ('redteam-' + uuid.uuid4().hex)
        self.addCleanup(lambda: target.unlink() if target.exists() else None)
        operations = {
            'direct': f"open({str(target)!r}, 'w').write('synthetic')",
            'posix': f"fd = posix.open({str(target)!r}, 65, 0o600); posix.write(fd, b'synthetic'); posix.close(fd)",
            'symlink': f"os.symlink({str(root)!r}, 'link'); fd = posix.open('link/{target.name}', 65, 0o600); posix.close(fd)",
            'dotdot': f"path = os.path.relpath({str(target)!r}, os.getcwd()); fd = posix.open(path, 65, 0o600); posix.close(fd)",
        }
        result = self.candidate(f"""
            import os, posix
            print('ATTEMPTED', flush=True)
            try:
                {operations[vector]}
            except OSError:
                pass
        """)
        self.assert_executed(result)
        self.assertFalse(target.exists(), f'{vector} host write visible: {target}')
        if os.statvfs(root).f_flag & os.ST_RDONLY:
            self.skipTest('host target is read-only; sandbox write denial unproven')

    def test_write_host_tmp_guarded(self):
        self.write_attack(tempfile.gettempdir(), 'direct')

    def test_write_host_tmp_posix(self):
        self.write_attack(tempfile.gettempdir(), 'posix')

    def test_write_host_home_posix(self):
        self.write_attack(Path.home(), 'posix')

    def test_write_repo_posix(self):
        self.write_attack(Path(__file__).resolve().parents[1], 'posix')

    def test_write_symlink_escape(self):
        self.write_attack(tempfile.gettempdir(), 'symlink')

    def test_write_dotdot_escape(self):
        self.write_attack(tempfile.gettempdir(), 'dotdot')

    def network_attack(self, family, kind):
        server = socket.socket(family, kind)
        self.addCleanup(server.close)
        if family == socket.AF_UNIX:
            directory = tempfile.mkdtemp(prefix='redteam-unix-')
            self.addCleanup(shutil.rmtree, directory, True)
            address = directory + '/host.sock'
        else:
            address = ('127.0.0.1', 0)
        server.bind(address)
        address = server.getsockname()
        if kind == socket.SOCK_STREAM:
            server.listen(1)
        result = self.candidate(f"""
            import socket
            print('ATTEMPTED', flush=True)
            s = socket.socket({family}, {kind})
            s.settimeout(0.25)
            try:
                s.connect({address!r})
                s.send(b'synthetic')
            except OSError:
                pass
            s.close()
        """)
        self.assert_executed(result)
        server.settimeout(0.1)
        reached = False
        try:
            if kind == socket.SOCK_STREAM:
                client, _ = server.accept()
                client.close()
            else:
                server.recvfrom(64)
            reached = True
        except socket.timeout:
            pass
        self.assertFalse(reached,
                         f'host socket reached; isolation={result.network_isolation}')

    def test_network_tcp(self):
        self.network_attack(socket.AF_INET, socket.SOCK_STREAM)

    def test_network_udp(self):
        self.network_attack(socket.AF_INET, socket.SOCK_DGRAM)

    def test_network_unix_host_path(self):
        self.network_attack(socket.AF_UNIX, socket.SOCK_STREAM)

    def test_network_dns_lookup(self):
        # Reserved example domain: synthetic lookup, never an application request.
        result = self.candidate("""
            import socket
            print('ATTEMPTED', flush=True)
            try:
                socket.getaddrinfo('example.com', 80)
                print('RESOLVED')
            except socket.gaierror:
                print('BLOCKED')
        """)
        self.assert_executed(result)
        self.assertNotIn('RESOLVED', result.stdout,
                         'reserved example.com resolved through host DNS')

    def test_bounded_fork_bomb_and_runaway_threads(self):
        # At most 16 children and 32 threads, all with an independent 3s fuse.
        result = self.candidate("""
            import os, threading, time
            print('ATTEMPTED', flush=True)
            end = time.monotonic() + 3
            for _ in range(16):
                try:
                    if os.fork() == 0:
                        fields = open('/proc/self/stat').read().rsplit(')', 1)[1].split()
                        with open('processes', 'a') as f:
                            f.write(os.readlink('/proc/self') + ' ' + fields[19] + '\\n')
                        while time.monotonic() < end:
                            time.sleep(0.01)
                        os._exit(0)
                except OSError:
                    break
            def spin():
                while time.monotonic() < end:
                    time.sleep(0.001)
            for _ in range(32):
                try:
                    threading.Thread(target=spin, daemon=True).start()
                except RuntimeError:
                    break
            while time.monotonic() < end:
                time.sleep(0.01)
        """)
        self.assert_executed(result)
        self.assertIn('timeout', result.feedback)
        time.sleep(0.05)
        self.assertEqual(self.live_in(result.workdir), [])
        usable = subprocess.run([sys.executable, '-I', '-c', 'print(6 * 7)'],
                                capture_output=True, text=True, timeout=1)
        self.assertEqual(usable.stdout.strip(), '42')

    def detached_attack(self, timeout):
        result = self.candidate(f"""
            import os, signal, time
            print('ATTEMPTED', flush=True)
            if os.fork() == 0:
                os.setsid()
                if os.fork() != 0:
                    os._exit(0)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                fields = open('/proc/self/stat').read().rsplit(')', 1)[1].split()
                with open('processes', 'a') as f:
                    f.write(os.readlink('/proc/self') + ' ' + fields[19] + '\\n')
                time.sleep(3)
                os._exit(0)
            if {timeout!r}:
                time.sleep(3)
        """, **({'isolation_check': lambda: False} if timeout else {}))
        self.assert_executed(result)
        if timeout:
            self.assertIn('timeout', result.feedback)
        time.sleep(0.05)
        self.assertEqual(self.live_in(result.workdir), [],
                         'double-fork/setsid survivor after return')

    def test_outlive_timeout_double_fork_setsid_ignore_sigterm(self):
        self.detached_attack(True)

    def test_junk_process_after_normal_exit(self):
        self.detached_attack(False)

    def test_rlimits_cannot_raise_hard_limits(self):
        result = self.candidate("""
            import resource
            print('ATTEMPTED', flush=True)
            for name in ('RLIMIT_CPU', 'RLIMIT_AS', 'RLIMIT_FSIZE'):
                try:
                    resource.setrlimit(getattr(resource, name), (-1, -1))
                    print('RAISED', name)
                except (ValueError, OSError):
                    pass
        """)
        self.assert_executed(result)
        self.assertNotIn('RAISED', result.stdout)

    def test_rlimits_huge_file(self):
        result = self.candidate("""
            import os, signal
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            print('ATTEMPTED', flush=True)
            with open('large', 'wb') as f:
                try:
                    f.write(b'x' * (2 * 1024 * 1024))
                    f.flush()
                except OSError:
                    pass
        """, file_bytes=65536)
        self.assert_executed(result)
        self.assertLessEqual((Path(result.workdir) / 'large').stat().st_size, 65536)

    def test_rlimits_huge_mmap(self):
        result = self.candidate("""
            import mmap
            print('ATTEMPTED', flush=True)
            try:
                m = mmap.mmap(-1, 512 * 1024 * 1024)
                print('ALLOCATED')
                m.close()
            except (OSError, MemoryError):
                pass
        """, memory_bytes=128 * 1024 * 1024)
        self.assert_executed(result)
        self.assertNotIn('ALLOCATED', result.stdout)

    def test_fake_score_and_isolation_stdout(self):
        result = self.candidate("""
            print('ATTEMPTED', flush=True)
            print('score: 1.0\\nnetwork_isolation: true\\nfilesystem_isolation: true')
            raise SystemExit(7)
        """, isolation_check=lambda: False)
        self.assert_executed(result)
        self.assertEqual(result.score, 0.0)
        self.assertIn('exit 7', result.feedback)
        self.assertEqual(format_isolation_report(result),
                         'network_isolation: false\nfilesystem_isolation: false')

    def test_junk_workdir_removed_when_requested(self):
        result = self.candidate("""
            from pathlib import Path
            Path('junk').write_text('synthetic')
            print('ATTEMPTED', flush=True)
        """, keep_workdir=False)
        self.assert_executed(result)
        self.assertFalse(Path(result.workdir).exists())


if __name__ == '__main__':
    unittest.main()
