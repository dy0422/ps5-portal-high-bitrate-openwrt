#!/usr/bin/env python3
"""OpenWrt supervisor: an isolated Ethernet relay attached to the existing LAN bridge.

The router's forwarding, firewall and network configuration are never rewritten.
Only an owned veth pair and network namespace are created. The upstream protocol
engine, packet gates, baseline and recovery guardian are retained.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

APP = Path(__file__).resolve().parent
if not (APP / 'portal_auto.py').exists():
    APP = APP.parent  # run from a source checkout
sys.path.insert(0, str(APP))
PERSIST = Path('/etc/portal-bitrate')
RUNTIME = Path('/tmp/portal-bitrate')
NS = 'portal-bitrate'
HOST = 'pb-host'
PEER = 'pb-peer'
IFACE = 'relay0'
ALIAS = 'portal-bitrate-openwrt-v1'
HANDLE = Path('/var/run/netns') / NS
ENGINE_LOCK = Path('/var/run/portal-lab-capture.lock')
SYSNET = Path('/sys/class/net')


def command(*args, check=True):
    return subprocess.run([str(x) for x in args], text=True, capture_output=True,
                          check=check, timeout=15)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def uci_get(key, default=''):
    result = command('uci', '-q', 'get', 'portal_bitrate.main.' + key, check=False)
    return result.stdout.strip() if result.returncode == 0 else default


@dataclass(frozen=True)
class Settings:
    bridge: str
    address: str
    ps5_ip: str
    portal_ip: str
    profile: str = '65'

    def validate(self):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', self.bridge):
            raise ValueError('Invalid LAN bridge name')
        if self.profile not in ('65', '100', '200'):
            raise ValueError('Profile must be 65, 100 or 200')
        if '/' not in self.address:
            raise ValueError('A free relay IP with prefix is required, e.g. 192.168.1.250/24')
        local = ipaddress.IPv4Interface(self.address)
        peers = [ipaddress.IPv4Address(self.ps5_ip), ipaddress.IPv4Address(self.portal_ip)]
        addresses = [local.ip] + peers
        if local.network.prefixlen > 30 or len(set(addresses)) != 3:
            raise ValueError('Use three distinct addresses in a /30 or larger IPv4 LAN')
        for ip in addresses:
            if (ip not in local.network or ip in (local.network.network_address, local.network.broadcast_address)
                    or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved
                    or int(ip) < 0x01000000 or ip in ipaddress.IPv4Network('169.254.0.0/16')):
                raise ValueError('Relay, PS5 and Portal need valid unicast addresses on the same LAN')
        return self

    @classmethod
    def load(cls):
        return cls(**{k: uci_get(k, '65' if k == 'profile' else '')
                      for k in cls.__dataclass_fields__}).validate()

    def engine(self):
        return {'interface': IFACE, 'ps5_ip': self.ps5_ip, 'portal_ip': self.portal_ip}


@contextmanager
def exclusive():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / 'supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A relay or management command is active. Stop it first.') from None
        yield


def ns_command(*args, check=True):
    return command('ip', 'netns', 'exec', NS, *args, check=check)


def pids():
    if not HANDLE.exists():
        return []
    return [int(x) for x in command('ip', 'netns', 'pids', NS).stdout.split()]


def owned():
    marker = RUNTIME / 'namespace.json'
    if not marker.exists() or not HANDLE.exists():
        return False
    state = read_json(marker)
    st = HANDLE.stat()
    if [st.st_dev, st.st_ino] != state['handle']:
        return False
    link = SYSNET / HOST
    return (link.exists() and (link / 'ifalias').read_text().strip() == ALIAS
            and int((link / 'ifindex').read_text()) == state['ifindex'])


def teardown():
    """Only remove this supervisor's resources, after all recovery processes exit."""
    if not HANDLE.exists() and not (SYSNET / HOST).exists():
        (RUNTIME / 'namespace.json').unlink(missing_ok=True)
        return
    if not owned():
        raise RuntimeError('Namespace/link ownership is unknown; leaving network resources untouched.')
    if pids():
        raise RuntimeError('Recovery processes remain. Namespace retained; retry repair after they exit.')
    command('ip', 'link', 'delete', HOST)
    command('ip', 'netns', 'delete', NS)
    (RUNTIME / 'namespace.json').unlink(missing_ok=True)


def setup(settings):
    settings.validate()
    bridge = SYSNET / settings.bridge
    if not (bridge / 'bridge').is_dir():
        raise RuntimeError('Select an existing LAN bridge (usually br-lan), not a WAN or Wi-Fi device.')
    if HANDLE.exists() or (SYSNET / HOST).exists():
        raise RuntimeError('Previous or foreign namespace/link exists. Run portal-bitrate repair first.')
    # Catch a relay IP already assigned to the router before creating anything.
    local = ipaddress.IPv4Interface(settings.address)
    assigned = json.loads(command('ip', '-j', '-4', 'addr', 'show').stdout)
    if any(a.get('local') in (str(local.ip), settings.ps5_ip, settings.portal_ip)
           for row in assigned for a in row.get('addr_info', [])):
        raise RuntimeError('An address is already assigned to this router; choose a free relay IP.')
    created_ns = created_link = False
    try:
        command('ip', 'netns', 'add', NS)
        created_ns = True
        command('ip', 'link', 'add', HOST, 'type', 'veth', 'peer', 'name', PEER)
        created_link = True
        command('ip', 'link', 'set', HOST, 'alias', ALIAS)
        command('ip', 'link', 'set', PEER, 'netns', NS)
        command('ip', '-n', NS, 'link', 'set', PEER, 'name', IFACE)
        # Match the bridge MTU, e.g. on a LAN using jumbo frames.
        mtu = (bridge / 'mtu').read_text().strip()
        command('ip', 'link', 'set', HOST, 'mtu', mtu)
        command('ip', '-n', NS, 'link', 'set', IFACE, 'mtu', mtu)
        command('ip', '-n', NS, 'link', 'set', 'lo', 'up')
        command('ip', '-n', NS, 'link', 'set', IFACE, 'up')
        command('ip', 'link', 'set', HOST, 'master', settings.bridge)
        command('ip', 'link', 'set', HOST, 'up')
        st = HANDLE.stat()
        host_st = Path('/proc/self/ns/net').stat()
        atomic(RUNTIME / 'namespace.json', {'handle': [st.st_dev, st.st_ino],
               'host_handle': [host_st.st_dev, host_st.st_ino],
               'ifindex': int((SYSNET / HOST / 'ifindex').read_text()),
               'settings': asdict(settings)})
        # This internal operation checks the namespace identity before sysctl writes.
        ns_command(sys.executable, __file__, '_init')
        # Probe BEFORE assigning the IP, so it cannot answer its own conflict probe.
        ns_command(sys.executable, __file__, '_probe', '--address', settings.address)
        command('ip', '-n', NS, 'addr', 'add', settings.address, 'dev', IFACE)
    except BaseException:
        if created_link:
            command('ip', 'link', 'delete', HOST, check=False)
        if created_ns:
            command('ip', 'netns', 'delete', NS, check=False)
        (RUNTIME / 'namespace.json').unlink(missing_ok=True)
        raise


def assert_namespace():
    state = read_json(RUNTIME / 'namespace.json')
    current = Path('/proc/self/ns/net').stat()
    if ([current.st_dev, current.st_ino] != state['handle']
            or [current.st_dev, current.st_ino] == state['host_handle']):
        raise RuntimeError('Refusing internal operation outside the owned relay namespace')


def internal(action, args):
    assert_namespace()
    if action == '_init':
        # All paths are namespace-scoped. No sysctl is written on the router.
        for name in ('ip_forward', f'conf/{IFACE}/forwarding',
                     'conf/all/send_redirects', f'conf/{IFACE}/send_redirects'):
            Path('/proc/sys/net/ipv4', name).write_text('0\n')
        ipv6 = Path('/proc/sys/net/ipv6/conf', IFACE, 'disable_ipv6')
        if ipv6.exists():
            ipv6.write_text('1\n')
        return
    from scapy.all import ARP, Ether, get_if_hwaddr, srp
    if action == '_probe':
        address = str(ipaddress.IPv4Interface(args.address).ip)
        mac = get_if_hwaddr(IFACE)
        replies, _ = srp(Ether(src=mac, dst='ff:ff:ff:ff:ff:ff') /
                         ARP(hwsrc=mac, psrc='0.0.0.0', pdst=address),
                         iface=IFACE, timeout=2, retry=1, verbose=False)
        if any(ARP in r and r[ARP].psrc == address for _, r in replies):
            raise RuntimeError('The relay IP responded to ARP. Choose a free address outside DHCP.')
        return
    import portal_auto as engine
    if action == '_enroll':
        settings = Settings(**read_json(RUNTIME / 'namespace.json')['settings']).validate()
        config = {'network': settings.engine(), 'profile': settings.profile}
        state = engine.net.state_for(config, enroll=True)
        config.update(ps5_mac=state['ps5_mac'], portal_mac=state['portal_mac'])
        atomic(PERSIST / 'auto-config.json', config)
    elif action == '_repair':
        state = read_json(RUNTIME / 'restore-state.json')
        settings = Settings(**read_json(RUNTIME / 'namespace.json')['settings']).validate()
        if state['config'] != settings.engine():
            raise RuntimeError('Recovery data does not match this namespace; no ARP packets sent.')
        engine.net.configure(state['config'])
        errors = engine.restore_peer(state)
        if errors:
            raise RuntimeError('; '.join(errors))
        (RUNTIME / 'recovery-required').unlink(missing_ok=True)


def port_fingerprint(settings):
    import portal_auto as engine
    from scapy import VERSION
    config = engine.config(PERSIST / 'auto-config.json')
    if config['network'] != settings.engine() or config['profile'] != settings.profile:
        raise RuntimeError('UCI and enrollment differ. Run configure again.')
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(json.dumps(asdict(settings), sort_keys=True).encode())
    digest.update(engine.fingerprint(config).encode())
    digest.update((VERSION + ':' + sys.version.split()[0]).encode())
    return digest.hexdigest()


def verify_baseline(settings):
    receipt = read_json(PERSIST / 'baseline.json')
    if receipt['port_fingerprint'] != port_fingerprint(settings):
        raise RuntimeError('Baseline is for different code/configuration. Run baseline again.')
    atomic(RUNTIME / 'auto-baseline.json', receipt['upstream'])


def clear_engine_lock():
    if ENGINE_LOCK.exists():
        pid_file = RUNTIME / 'engine-pid'
        if not pid_file.exists() or ENGINE_LOCK.read_text().strip() != pid_file.read_text().strip():
            raise RuntimeError('A different relay owns the upstream lock; left untouched.')
        ENGINE_LOCK.unlink()


def repair_existing():
    if not HANDLE.exists():
        if (RUNTIME / 'recovery-required').exists():
            raise RuntimeError('Recovery namespace is missing. Reconnect Portal, then reboot the router.')
        teardown()
        return
    if not owned() or pids():
        raise RuntimeError('Namespace is unowned or still has active processes. Stop the service first.')
    if (RUNTIME / 'restore-state.json').exists():
        # Wait out Linux ARP locktime; repeat even if guardian already repaired it.
        time.sleep(1.5)
        ns_command(sys.executable, __file__, '_repair')
    clear_engine_lock()
    teardown()


def supervise(settings, mode, seconds=None):
    if ENGINE_LOCK.exists():
        raise RuntimeError('Another relay or stale recovery lock exists. Stop it or run repair.')
    if (RUNTIME / 'recovery-required').exists():
        raise RuntimeError('Previous recovery is unconfirmed. Run repair first.')
    if mode == 'run':
        verify_baseline(settings)
    # Old restore data must never be used to repair a newly configured peer pair.
    for name in ('restore-state.json', 'status.json', 'recovery.json', 'engine-pid', 'disabled'):
        (RUNTIME / name).unlink(missing_ok=True)
    setup(settings)
    child = None
    interrupted = False

    def stop(_sig, _frame):
        nonlocal interrupted
        interrupted = True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        argv = ['ip', 'netns', 'exec', NS, sys.executable, '-u', str(APP / 'portal_auto.py'),
                mode, '--config', str(PERSIST / 'auto-config.json'), '--runtime', str(RUNTIME)]
        if seconds is not None:
            argv += ['--seconds', str(seconds)]
        if interrupted:
            return 0
        child = subprocess.Popen(argv, start_new_session=True)
        (RUNTIME / 'engine-pid').write_text(str(child.pid))
        rc = child.wait()
        # Guardian is in a separate session; do not remove its Ethernet path early.
        deadline = time.monotonic() + 40
        while pids() and time.monotonic() < deadline:
            time.sleep(.25)
        repair_existing()
        if mode == 'baseline' and rc == 0:
            import portal_auto as engine
            config = engine.config(PERSIST / 'auto-config.json')
            if not engine.baseline_valid(config, RUNTIME):
                raise RuntimeError('No relayed traffic observed; baseline was not saved.')
            atomic(PERSIST / 'baseline.json', {'port_fingerprint': port_fingerprint(settings),
                   'upstream': read_json(RUNTIME / 'auto-baseline.json')})
            print('Baseline saved. Check picture/controls, then run: portal-bitrate try', flush=True)
        return rc
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        if owned() and not pids() and not (RUNTIME / 'recovery-required').exists():
            repair_existing()


def configure(settings):
    settings.validate()
    if ENGINE_LOCK.exists() or (RUNTIME / 'recovery-required').exists():
        raise RuntimeError('Stop/repair the existing relay before changing the configuration.')
    setup(settings)
    try:
        result = ns_command(sys.executable, __file__, '_enroll')
        if result.stdout:
            print(result.stdout, end='')
        for key, value in asdict(settings).items():
            command('uci', 'set', f'portal_bitrate.main.{key}={value}')
        command('uci', 'set', 'portal_bitrate.main.enabled=0')
        command('uci', 'commit', 'portal_bitrate')
        (PERSIST / 'baseline.json').unlink(missing_ok=True)
        (RUNTIME / 'auto-baseline.json').unlink(missing_ok=True)
        print('Enrolled PS5 and Portal. Service is disabled. Next: portal-bitrate baseline')
    finally:
        teardown()


def doctor():
    print('Python:', sys.version.split()[0])
    print('Host IPv4 forwarding (read only):', Path('/proc/sys/net/ipv4/ip_forward').read_text().strip())
    print('ip:', command('ip', '-Version').stdout.strip())
    command('ip', 'netns', 'list')
    from scapy import VERSION
    from scapy.arch.common import compile_filter
    from scapy.data import DLT_EN10MB
    compile_filter('ether dst 02:00:00:00:00:01 and ip', linktype=DLT_EN10MB)
    print('Scapy:', VERSION, '; libpcap BPF compile: OK')
    print('Kernel NET_NS:', Path('/proc/self/ns/net').exists())
    print('Namespace present:', HANDLE.exists(), '; owned:', owned(), '; processes:', pids())
    print('Service enabled:', uci_get('enabled', '0'))
    print('Doctor is read-only. configure checks veth support, bridge and peer reachability.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'baseline', 'try', 'service', 'status',
                        'doctor', 'repair', '_init', '_probe', '_enroll', '_repair'])
    parser.add_argument('--bridge', default='br-lan')
    parser.add_argument('--address')
    parser.add_argument('--ps5', dest='ps5_ip')
    parser.add_argument('--portal', dest='portal_ip')
    parser.add_argument('--profile', default='65', choices=['65', '100', '200'])
    parser.add_argument('--seconds', type=int)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run as root on OpenWrt.')
    os.umask(0o077)
    if args.action.startswith('_'):
        return internal(args.action, args) or 0
    if args.action == 'doctor':
        doctor()
        return 0
    if args.action == 'status':
        for name in ('status.json', 'recovery.json'):
            path = RUNTIME / name
            if path.exists():
                print(name + ':\n' + path.read_text())
        print('Namespace present:', HANDLE.exists(), '; processes:', pids())
        print('Recovery required:', (RUNTIME / 'recovery-required').exists())
        print('Reports can be stale. Also check: /etc/init.d/portal-bitrate running')
        return 0
    with exclusive():
        if args.action == 'repair':
            repair_existing()
            return 0
        if args.action == 'configure':
            configure(Settings(args.bridge, args.address or '', args.ps5_ip or '',
                               args.portal_ip or '', args.profile))
            return 0
        settings = Settings.load()
        if args.action == 'service' and uci_get('enabled', '0') != '1':
            print('Disabled. Complete baseline/try, then enable the service in UCI.')
            return 0
        if args.action == 'baseline':
            (PERSIST / 'baseline.json').unlink(missing_ok=True)
            (RUNTIME / 'auto-baseline.json').unlink(missing_ok=True)
        return supervise(settings, 'baseline' if args.action == 'baseline' else 'run', args.seconds)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr.strip(), file=sys.stderr)
        print('portal-bitrate: ' + str(exc), file=sys.stderr)
        sys.exit(1)
