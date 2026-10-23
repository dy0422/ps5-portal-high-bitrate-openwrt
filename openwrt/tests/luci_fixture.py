"""Disposable-container LAN with fake peers for the browser integration test."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from netns_smoke import DEVICES, cmd, fixture


def main():
    if os.environ.get('PORTAL_TEST_DISPOSABLE') != '1' or os.geteuid() != 0:
        raise SystemExit('Disposable root container only')
    if Path('/sys/class/net/br-lan').exists():
        raise SystemExit('Test bridge already exists')
    peers, running = [], True
    def stop(_sig, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix='pb-ui-') as td:
        td = Path(td)
        try:
            Path('/proc/sys/net/ipv4/ip_forward').write_text('1\n')
            cmd('ip', 'link', 'add', 'br-lan', 'type', 'bridge')
            cmd('ip', 'link', 'set', 'br-lan', 'up')
            cmd('ip', 'addr', 'add', '10.77.0.1/24', 'dev', 'br-lan')
            for index, (ns, (address, mac)) in enumerate(DEVICES.items()):
                cmd('ip', 'netns', 'add', ns)
                cmd('ip', 'link', 'add', 'pbui' + str(index), 'type', 'veth', 'peer', 'name', 'eth0', 'netns', ns)
                cmd('ip', 'link', 'set', 'pbui' + str(index), 'master', 'br-lan', 'up')
                cmd('ip', '-n', ns, 'link', 'set', 'eth0', 'address', mac, 'up')
                cmd('ip', '-n', ns, 'addr', 'add', address + '/24', 'dev', 'eth0')
                cmd('ip', 'netns', 'exec', ns, 'ethtool', '-K', 'eth0', 'tx', 'off')
            (td / 'startup.bin').write_bytes(fixture())
            (td / 'server.py').write_text('''import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('10.77.0.10',9296))
while True:
 d,a=s.recvfrom(65535);s.sendto(b'video'*200,a)
''')
            (td / 'peer.py').write_text('''import socket,sys,time
p=open(sys.argv[1],'rb').read();s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(('10.77.0.20',50000));s.settimeout(.2)
while True:
 s.sendto(p,('10.77.0.10',9296))
 try:s.recv(65535)
 except socket.timeout:pass
 time.sleep(.2)
''')
            peers.append(subprocess.Popen(['ip', 'netns', 'exec', 'pb-ps5', 'python3', str(td / 'server.py')]))
            peers.append(subprocess.Popen(['ip', 'netns', 'exec', 'pb-portal', 'python3', str(td / 'peer.py'), str(td / 'startup.bin')]))
            print('FIXTURE READY', flush=True)
            while running:
                time.sleep(.2)
        finally:
            for peer in peers:
                peer.terminate()
                peer.wait(timeout=5)
            for ns in DEVICES:
                cmd('ip', 'netns', 'delete', ns, check=False)
            cmd('ip', 'link', 'delete', 'br-lan', check=False)


if __name__ == '__main__':
    main()
