import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import portal_openwrt as port


class PortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, value in [('RUNTIME', self.root / 'run'), ('PERSIST', self.root / 'etc'),
                            ('SYSNET', self.root / 'sys'), ('HANDLE', self.root / 'netns'),
                            ('ENGINE_LOCK', self.root / 'engine.lock')]:
            p = patch.object(port, name, value)
            p.start()
            self.addCleanup(p.stop)
        port.RUNTIME.mkdir()
        port.PERSIST.mkdir()
        bridge = port.SYSNET / 'br-lan'
        (bridge / 'bridge').mkdir(parents=True)
        (bridge / 'mtu').write_text('1500')
        self.settings = port.Settings('br-lan', '192.168.1.250/24', '192.168.1.100', '192.168.1.101')
        self.addCleanup(self.temp.cleanup)

    def fake_command(self, *args, check=True):
        if args[:3] == ('ip', '-j', '-4'):
            return subprocess.CompletedProcess(args, 0, '[]', '')
        if args[:4] == ('ip', 'netns', 'add', port.NS):
            port.HANDLE.touch()
        if args[:4] == ('ip', 'link', 'add', port.HOST):
            link = port.SYSNET / port.HOST
            link.mkdir()
            (link / 'ifindex').write_text('123')
            (link / 'ifalias').write_text(port.ALIAS)
        return subprocess.CompletedProcess(args, 0, '', '')

    def prepare_owned(self):
        with patch.object(port, 'command', side_effect=self.fake_command), patch.object(port, 'ns_command'):
            port.setup(self.settings)
        self.assertTrue(port.owned())

    def test_valid_and_invalid_network_configuration(self):
        self.assertEqual(self.settings.validate(), self.settings)
        for change in ({'bridge': 'br-lan;reboot'}, {'address': '192.168.1.250'},
                       {'portal_ip': '192.168.2.2'}, {'portal_ip': '192.168.1.250'},
                       {'ps5_ip': '192.168.1.0'}, {'address': '192.168.1.255/24'},
                       {'profile': '999'}, {'ps5_ip': '127.0.0.1'},
                       {'address': '0.1.1.5/8', 'ps5_ip': '0.1.1.2', 'portal_ip': '0.1.1.3'}):
            values = port.asdict(self.settings)
            values.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                port.Settings(**values).validate()

    def test_exclusive_lock_blocks_concurrent_configuration(self):
        with port.exclusive():
            with self.assertRaisesRegex(RuntimeError, 'active'):
                with port.exclusive():
                    self.fail('Concurrent command entered')

    def test_setup_isolates_forwarding_and_probes_before_ip_assignment(self):
        events = []
        def cmd(*args, **kw):
            events.append(args)
            return self.fake_command(*args, **kw)
        def ns(*args):
            events.append(('namespace',) + args)
        with patch.object(port, 'command', side_effect=cmd), patch.object(port, 'ns_command', side_effect=ns):
            port.setup(self.settings)
        self.assertTrue(port.owned())
        init = next(i for i, a in enumerate(events) if '_init' in a)
        probe = next(i for i, a in enumerate(events) if '_probe' in a)
        assign = next(i for i, a in enumerate(events) if a[:5] == ('ip', '-n', port.NS, 'addr', 'add'))
        self.assertLess(init, probe)
        self.assertLess(probe, assign)
        self.assertFalse(any('sysctl' in a or 'iptables' in a or 'nft' in a for a in events))
        self.assertFalse(any('default' in a for a in events))

    def test_setup_failure_rolls_back_without_assigning_address(self):
        with patch.object(port, 'command', side_effect=self.fake_command) as cmd:
            with patch.object(port, 'ns_command', side_effect=[None, RuntimeError('IP conflict')]):
                with self.assertRaisesRegex(RuntimeError, 'IP conflict'):
                    port.setup(self.settings)
        calls = [c.args for c in cmd.call_args_list]
        self.assertIn(('ip', 'link', 'delete', port.HOST), calls)
        self.assertIn(('ip', 'netns', 'delete', port.NS), calls)
        self.assertFalse(any('addr' in c and 'add' in c for c in calls))
        self.assertFalse((port.RUNTIME / 'namespace.json').exists())

    def test_router_address_conflict_stops_before_any_network_mutation(self):
        data = '[{"addr_info":[{"local":"192.168.1.250"}]}]'
        with patch.object(port, 'command', return_value=MagicMock(stdout=data)) as cmd:
            with self.assertRaisesRegex(RuntimeError, 'already assigned'):
                port.setup(self.settings)
        self.assertEqual(cmd.call_count, 1)

    def test_foreign_namespace_never_removed(self):
        port.HANDLE.touch()
        with patch.object(port, 'command') as cmd:
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                port.teardown()
            cmd.assert_not_called()

    def test_guardian_keeps_ethernet_path_alive(self):
        self.prepare_owned()
        with patch.object(port, 'pids', return_value=[123]), patch.object(port, 'command') as cmd:
            with self.assertRaisesRegex(RuntimeError, 'processes remain'):
                port.teardown()
            cmd.assert_not_called()

    def test_repairs_before_deleting_namespace(self):
        self.prepare_owned()
        (port.RUNTIME / 'restore-state.json').write_text('{}')
        events = []
        with patch.object(port, 'pids', return_value=[]), patch.object(port.time, 'sleep'):
            with patch.object(port, 'ns_command', side_effect=lambda *a: events.append('repair')):
                with patch.object(port, 'teardown', side_effect=lambda: events.append('delete')):
                    port.repair_existing()
        self.assertEqual(events, ['repair', 'delete'])

    def test_failed_repair_preserves_namespace_and_lock(self):
        self.prepare_owned()
        (port.RUNTIME / 'restore-state.json').write_text('{}')
        port.ENGINE_LOCK.write_text('123')
        with patch.object(port, 'pids', return_value=[]), patch.object(port.time, 'sleep'):
            with patch.object(port, 'ns_command', side_effect=RuntimeError('adapter down')):
                with self.assertRaisesRegex(RuntimeError, 'adapter down'):
                    port.repair_existing()
        self.assertTrue(port.HANDLE.exists())
        self.assertTrue(port.ENGINE_LOCK.exists())

    def test_foreign_engine_lock_never_removed(self):
        port.ENGINE_LOCK.write_text('999')
        (port.RUNTIME / 'engine-pid').write_text('123')
        with self.assertRaisesRegex(RuntimeError, 'different relay'):
            port.clear_engine_lock()
        self.assertEqual(port.ENGINE_LOCK.read_text(), '999')
        port.ENGINE_LOCK.write_text('123')
        port.clear_engine_lock()
        self.assertFalse(port.ENGINE_LOCK.exists())

    def test_internal_operations_refuse_host_namespace(self):
        stat = Path('/proc/self/ns/net').stat()
        port.atomic(port.RUNTIME / 'namespace.json', {'handle': [stat.st_dev, stat.st_ino],
                                                     'host_handle': [stat.st_dev, stat.st_ino]})
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            port.internal('_init', MagicMock())

    def test_active_run_refused_before_baseline_or_failed_recovery(self):
        with patch.object(port, 'setup') as setup:
            with self.assertRaises(FileNotFoundError):
                port.supervise(self.settings, 'run')
            (port.RUNTIME / 'recovery-required').touch()
            with self.assertRaisesRegex(RuntimeError, 'recovery'):
                port.supervise(self.settings, 'baseline')
            setup.assert_not_called()

    def test_runtime_receipt_survives_reboot_and_binds_port_settings(self):
        import portal_auto as engine
        cfg = {'network': self.settings.engine(), 'profile': '65',
               'ps5_mac': '02:00:00:00:00:02', 'portal_mac': '02:00:00:00:00:03'}
        port.atomic(port.PERSIST / 'auto-config.json', cfg)
        receipt = {'fingerprint': engine.fingerprint(cfg), 'forwarded_out': 3, 'restored': True}
        port.atomic(port.PERSIST / 'baseline.json',
                    {'port_fingerprint': port.port_fingerprint(self.settings), 'upstream': receipt})
        port.verify_baseline(self.settings)
        self.assertTrue(engine.baseline_valid(cfg, port.RUNTIME))
        other = port.Settings('br-other', self.settings.address, self.settings.ps5_ip, self.settings.portal_ip)
        with self.assertRaisesRegex(RuntimeError, 'different code/configuration'):
            port.verify_baseline(other)

    def test_term_is_forwarded_to_exact_child_and_recovery_finishes(self):
        child = MagicMock(pid=123)
        child.poll.return_value = None
        child.wait.side_effect = lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None) or 0
        with patch.object(port, 'verify_baseline'), patch.object(port, 'setup'):
            with patch.object(port.subprocess, 'Popen', return_value=child):
                with patch.object(port, 'pids', return_value=[]), patch.object(port, 'owned', return_value=False):
                    with patch.object(port, 'repair_existing') as repair:
                        self.assertEqual(port.supervise(self.settings, 'run'), 0)
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        repair.assert_called_once()


class PackageTests(unittest.TestCase):
    def test_package_and_bundle_integrity(self):
        import gzip
        import hashlib
        import io
        import tarfile
        import build
        with tempfile.TemporaryDirectory() as td, patch.object(build, 'OUT', Path(td)):
            ipk, bundle = build.build()
            with tarfile.open(ipk, 'r:gz') as outer:
                self.assertEqual(set(outer.getnames()), {'./debian-binary', './control.tar.gz', './data.tar.gz'})
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile('./data.tar.gz').read())) as data:
                    names = data.getnames()
                    self.assertIn('./usr/lib/portal-bitrate/portal_auto.py', names)
                    self.assertIn('./usr/lib/portal-bitrate/portal_openwrt.py', names)
                    self.assertEqual(data.getmember('./usr/lib/portal-bitrate').mode, 0o700)
                    self.assertEqual(data.getmember('./etc/config/portal_bitrate').mode, 0o600)
                    self.assertIn(b"option enabled '0'", data.extractfile('./etc/config/portal_bitrate').read())
                    self.assertFalse(any('auto-config.json' in n or 'baseline.json' in n or '.venv' in n for n in names))
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile('./control.tar.gz').read())) as ctrl:
                    text = ctrl.extractfile('./control').read().decode()
                    self.assertIn('Architecture: all', text)
                    self.assertIn('scapy, ip-full, kmod-veth, libpcap', text)
                    for n in ('preinst', 'postinst', 'prerm', 'postrm'):
                        subprocess.run(['sh', '-n'], input=ctrl.extractfile('./' + n).read(), check=True)
            with tarfile.open(bundle, 'r:gz') as tar:
                base = './portal-bitrate-openwrt/'
                manifest = json.loads(tar.extractfile(base + 'MANIFEST.json').read())
                for n, expected in manifest.items():
                    self.assertEqual(hashlib.sha256(tar.extractfile(base + n).read()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
