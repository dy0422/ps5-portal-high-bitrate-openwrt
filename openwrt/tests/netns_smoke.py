"""Exercise the installed supervisor against fake devices on an isolated LAN."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

BRIDGE = 'br-pbtest'
DEVICES = {'pb-ps5': ('10.77.0.10', '02:77:00:00:00:02'),
           'pb-portal': ('10.77.0.20', '02:77:00:00:00:03')}
APP = Path('/usr/lib/portal-bitrate')
RT = Path('/tmp/portal-bitrate')


def cmd(*args, check=True, **kw):
    return subprocess.run(args, check=check, text=True, capture_output=True, **kw)


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError('Timed out waiting for test condition')


def fixture():
    def varint(x):
        b = bytearray()
        while x > 127:
            b.append((x & 127) | 128)
            x >>= 7
        return bytes(b) + bytes([x])
    def field(k, v):
        return (varint(k * 8) + varint(v) if isinstance(v, int)
                else varint(k * 8 + 2) + varint(len(v)) + v)
    plain = b' ' * 334 + b'25000' + b' ' * (1721 - 339)
    cipher = bytes(a ^ ((i * 19 + 31) % 256) for i, a in enumerate(plain))
    msg = field(1, 0) + field(2, field(1, 20) + field(2, b'synthetic') +
                              field(3, base64.b64encode(cipher)) + field(4, b''))
    data = (7).to_bytes(4, 'big') + b'\0' * 5 + msg[:1200]
    return b'\0tagg' + b'\0' * 10 + (len(data) + 4).to_bytes(2, 'big') + data


def main():
    if os.geteuid() != 0 or os.environ.get('PORTAL_TEST_DISPOSABLE') != '1':
        raise SystemExit('Disposable root container only')
    if not APP.exists() or not Path('/usr/sbin/portal-bitrate').exists():
        raise SystemExit('Install the test package first')
    if Path('/var/run/netns/portal-bitrate').exists() or any(
            Path('/var/run/netns', ns).exists() for ns in DEVICES):
        raise SystemExit('Conflicting test namespaces already exist')
    if Path('/sys/class/net', BRIDGE).exists():
        raise SystemExit('Conflicting test bridge exists')
    processes = []
    created = []
    forwarding = Path('/proc/sys/net/ipv4/ip_forward')
    old_forwarding = forwarding.read_text()
    try:
        forwarding.write_text('1\n')
        cmd('ip', 'link', 'add', BRIDGE, 'type', 'bridge')
        cmd('ip', 'link', 'set', BRIDGE, 'up')
        cmd('ip', 'addr', 'add', '10.77.0.1/24', 'dev', BRIDGE)
        for index, (ns, (address, mac)) in enumerate(DEVICES.items()):
            cmd('ip', 'netns', 'add', ns)
            created.append(ns)
            link = 'pbt' + str(index)
            cmd('ip', 'link', 'add', link, 'type', 'veth', 'peer', 'name', 'eth0', 'netns', ns)
            cmd('ip', 'link', 'set', link, 'master', BRIDGE, 'up')
            cmd('ip', '-n', ns, 'link', 'set', 'lo', 'up')
            cmd('ip', '-n', ns, 'link', 'set', 'eth0', 'address', mac, 'up')
            cmd('ip', '-n', ns, 'addr', 'add', address + '/24', 'dev', 'eth0')
            cmd('ip', 'netns', 'exec', ns, 'ethtool', '-K', 'eth0', 'tx', 'off')
        # Explicitly acknowledge this overwrites only the disposable container's test config.
        Path('/etc/config/portal_bitrate').write_text("config relay 'main'\n option enabled '0'\n")
        cmd('portal-bitrate', 'configure', '--bridge', BRIDGE, '--address', '10.77.0.250/24',
            '--ps5', '10.77.0.10', '--portal', '10.77.0.20', '--profile', '65')
        assert not Path('/var/run/netns/portal-bitrate').exists()
        print('PASS: enrollment; temporary namespace removed', flush=True)
        with tempfile.TemporaryDirectory(prefix='pb-smoke-') as td:
            td = Path(td)
            (td / 'startup.bin').write_bytes(fixture())
            server = td / 'server.py'
            server.write_text('''import hashlib, socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('10.77.0.10', 9296))
with open(sys.argv[1], 'a', buffering=1) as log:
 while True:
  data, peer = s.recvfrom(65535)
  log.write(hashlib.sha256(data).hexdigest() + '\\n')
  s.sendto(b'video' * 200, peer)
''')
            peer = td / 'peer.py'
            peer.write_text('''import socket, sys, time
payload = open(sys.argv[1], 'rb').read()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('10.77.0.20', 50000))
s.settimeout(.4)
received = 0
for _ in range(int(sys.argv[2])):
 s.sendto(payload, ('10.77.0.10', 9296))
 try:
  s.recv(65535); received += 1
 except socket.timeout: pass
 time.sleep(.15)
print(received)
''')
            plain = td / 'plain.bin'
            plain.write_bytes(b'controller-feedback')
            server_log = td / 'ps5.log'
            proc = subprocess.Popen(['ip', 'netns', 'exec', 'pb-ps5', 'python3', str(server), str(server_log)])
            processes.append(proc)
            time.sleep(.4)

            def send(path, count=10):
                return int(cmd('ip', 'netns', 'exec', 'pb-portal', 'python3', str(peer), str(path), str(count)).stdout.strip())

            def start(mode):
                log = td / (mode + str(len(processes)) + '.log')
                out = log.open('w')
                p = subprocess.Popen(['portal-bitrate', mode], stdout=out, stderr=subprocess.STDOUT)
                processes.append(p)
                wait_for(lambda: 'READY' in log.read_text() or p.poll() is not None)
                assert p.poll() is None, log.read_text()
                return p, log

            def redirected():
                send(plain, 1)
                return '02:77:00:00:00:02' not in cmd('ip', '-n', 'pb-portal', 'neigh', 'show', '10.77.0.10').stdout

            assert send(plain) > 0
            baseline, log = start('baseline')
            wait_for(redirected)
            assert send(plain) > 0
            assert baseline.wait(timeout=75) == 0, log.read_text()
            assert Path('/etc/portal-bitrate/baseline.json').exists()
            assert forwarding.read_text().strip() == '1'
            assert not Path('/var/run/netns/portal-bitrate').exists()
            print('PASS: 60-second unchanged baseline; host forwarding remains on', flush=True)

            active, log = start('try')
            wait_for(redirected)
            assert send(td / 'startup.bin') > 0
            time.sleep(.2)
            status = json.loads((RT / 'status.json').read_text())
            wait_for(lambda: json.loads((RT / 'status.json').read_text())['counts'].get('unique_modified_startups', 0) > 0)
            import sys
            sys.path.insert(0, str(APP))
            import portal_active_probe as probe
            expected, changed = probe.mutate(fixture())
            assert changed
            assert hashlib.sha256(expected).hexdigest() in server_log.read_text()
            # The fake PS5's video/reply must still target the Portal's actual MAC.
            assert DEVICES['pb-portal'][1] in cmd('ip', '-n', 'pb-ps5', 'neigh', 'show', '10.77.0.20').stdout
            active.send_signal(signal.SIGTERM)
            assert active.wait(timeout=60) == 0, log.read_text()
            assert DEVICES['pb-ps5'][1] in cmd('ip', '-n', 'pb-portal', 'neigh', 'show', '10.77.0.10').stdout
            assert not Path('/var/run/netns/portal-bitrate').exists()
            assert forwarding.read_text().strip() == '1'
            print('PASS: real UDP mutation/delivery; direct return path; graceful stop/ARP restore', flush=True)

            active, log = start('try')
            wait_for(redirected)
            os.kill(int((RT / 'engine-pid').read_text()), signal.SIGKILL)
            assert active.wait(timeout=60) != 0
            assert DEVICES['pb-ps5'][1] in cmd('ip', '-n', 'pb-portal', 'neigh', 'show', '10.77.0.10').stdout
            assert not Path('/var/run/netns/portal-bitrate').exists()
            assert not (RT / 'recovery-required').exists()
            assert forwarding.read_text().strip() == '1'
            print('PASS: killed engine recovered; no namespace leak; router forwarding unchanged', flush=True)
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        # Test-only cleanup in a disposable container, for resources created above.
        for ns in reversed(created):
            cmd('ip', 'netns', 'delete', ns, check=False)
        cmd('ip', 'link', 'delete', BRIDGE, check=False)
        forwarding.write_text(old_forwarding)


if __name__ == '__main__':
    main()
