"""Run after netns_smoke.py, inside the same disposable OpenWrt container."""
import json
import os
from pathlib import Path
import subprocess
import time
from netns_smoke import APP, BRIDGE, DEVICES, RT, cmd, wait_for


def main():
    if os.environ.get('PORTAL_TEST_DISPOSABLE') != '1' or os.geteuid() != 0:
        raise SystemExit('Disposable root OpenWrt container only')
    if not Path('/etc/portal-bitrate/baseline.json').exists():
        raise SystemExit('Run netns_smoke.py first')
    if Path('/sys/class/net', BRIDGE).exists():
        raise SystemExit('Conflicting test bridge exists')
    created = []
    try:
        cmd('ip', 'link', 'add', BRIDGE, 'type', 'bridge')
        cmd('ip', 'link', 'set', BRIDGE, 'up')
        cmd('ip', 'addr', 'add', '10.77.0.1/24', 'dev', BRIDGE)
        for index, (ns, (address, mac)) in enumerate(DEVICES.items()):
            cmd('ip', 'netns', 'add', ns)
            created.append(ns)
            link = 'pbt' + str(index)
            cmd('ip', 'link', 'add', link, 'type', 'veth', 'peer', 'name', 'eth0', 'netns', ns)
            cmd('ip', 'link', 'set', link, 'master', BRIDGE, 'up')
            cmd('ip', '-n', ns, 'link', 'set', 'eth0', 'address', mac, 'up')
            cmd('ip', '-n', ns, 'addr', 'add', address + '/24', 'dev', 'eth0')
        cmd('uci', 'set', 'portal_bitrate.main.enabled=1')
        cmd('uci', 'commit', 'portal_bitrate')
        cmd('/etc/init.d/portal-bitrate', 'enable')
        cmd('/etc/init.d/portal-bitrate', 'start')
        wait_for(lambda: Path('/var/run/netns/portal-bitrate').exists() and
                 (RT / 'status.json').exists() and json.loads((RT / 'status.json').read_text()).get('state') == 'running')
        cmd('/etc/init.d/portal-bitrate', 'running')
        cmd('/etc/init.d/portal-bitrate', 'stop')
        wait_for(lambda: not Path('/var/run/netns/portal-bitrate').exists(), seconds=60)
        assert not (RT / 'recovery-required').exists()
        assert Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1'
        print('PASS: actual procd registration/start/stop and guardian cleanup', flush=True)
    finally:
        cmd('uci', 'set', 'portal_bitrate.main.enabled=0', check=False)
        cmd('uci', 'commit', 'portal_bitrate', check=False)
        cmd('/etc/init.d/portal-bitrate', 'stop', check=False)
        cmd('/etc/init.d/portal-bitrate', 'disable', check=False)
        for ns in reversed(created):
            cmd('ip', 'netns', 'delete', ns, check=False)
        cmd('ip', 'link', 'delete', BRIDGE, check=False)


if __name__ == '__main__':
    main()
