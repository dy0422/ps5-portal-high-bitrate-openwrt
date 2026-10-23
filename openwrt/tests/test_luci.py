import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import portal_luci as api
import portal_openwrt as port


class LuCITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for module, name, value in [(api, 'JOBS', root / 'jobs'),
                                    (port, 'RUNTIME', root / 'run'),
                                    (port, 'PERSIST', root / 'etc'),
                                    (port, 'SYSNET', root / 'net')]:
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)
            value.mkdir()
        self.settings = dict(bridge='br-lan', address='192.168.1.250/24',
                             ps5_ip='192.168.1.100', portal_ip='192.168.1.101', profile='65')

    def job(self, action='baseline', **values):
        job = dict(id='a' * 32, action=action, state='running', request={}, pid=100, identity='123')
        if action == 'start':
            job['request'] = {'autostart': 0}
        job.update(values)
        port.atomic(api.JOBS / 'job.json', job)
        return job

    def test_only_allowlisted_methods_and_arguments(self):
        with self.assertRaises(ValueError):
            api.validate('exec', {'command': 'reboot'})
        for request in ({'autostart': 2}, {'autostart': True}, {'autostart': '1'}, {'autostart': 0, 'command': 'reboot'}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                api.validate('start', request)
        with self.assertRaises(ValueError):
            api.validate('repair', {'path': '/etc/shadow'})
        self.assertEqual(api.validate('stop', {'ubus_rpc_session': 'ignored'}), {})

    def test_configuration_rejected_before_launch(self):
        for setting in ({'bridge': 'br-lan; reboot'}, {'address': '/etc/shadow'}, {'profile': '500'}):
            request = dict(self.settings, **setting)
            with patch.object(api.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    api.dispatch('configure', request)
                spawn.assert_not_called()

    def test_async_job_has_only_fixed_executable_arguments(self):
        with patch.object(api.subprocess, 'Popen', return_value=MagicMock(pid=4242)) as spawn:
            with patch.object(api, 'process_identity', return_value='123'):
                result = api.dispatch('configure', self.settings)
        self.assertTrue(result['ok'])
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[-2:], ['worker', result['id']])
        self.assertEqual(argv[0], sys.executable)
        self.assertNotIn('shell', spawn.call_args.kwargs)
        self.assertEqual(api.optional(api.JOBS / 'job.json')['request'], self.settings)

    def test_overlapping_job_refused_but_stop_is_queued(self):
        job = self.job()
        with patch.object(api, 'alive', return_value=True), patch.object(api.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, '正在进行'):
                api.dispatch('doctor', {})
            result = api.dispatch('stop', {})
            self.assertTrue(result['stopping'])
            self.assertTrue((api.JOBS / ('cancel.' + job['id'])).exists())
            spawn.assert_not_called()

    def test_orphaned_process_prevents_new_jobs(self):
        self.job(child_pid=200, child_identity='222')
        with patch.object(api, 'alive', side_effect=lambda pid, identity: pid == 200):
            with self.assertRaisesRegex(RuntimeError, '仍在运行'):
                api.dispatch('configure', self.settings)

    def test_stop_captures_orphan_identity_without_accepting_user_pid(self):
        self.job(child_pid=200, child_identity='222')
        with patch.object(api, 'alive', side_effect=lambda pid, identity: pid == 200):
            with patch.object(api.subprocess, 'Popen', return_value=MagicMock(pid=4242)):
                api.dispatch('stop', {})
        self.assertEqual(api.optional(api.JOBS / 'job.json')['orphan'],
                         {'pid': 200, 'identity': '222', 'action': 'baseline'})

    def test_dead_worker_reported_as_failure(self):
        self.job()
        with patch.object(api, 'alive', return_value=False):
            self.assertEqual(api.current_job()['state'], 'failed')

    def test_process_identity_rejects_pid_reuse(self):
        identity = api.process_identity(os.getpid())
        self.assertTrue(api.alive(os.getpid(), identity))
        self.assertFalse(api.alive(os.getpid(), 'not-the-start-time'))

    def test_status_rejects_stale_running_record(self):
        port.atomic(port.RUNTIME / 'status.json', {'state': 'running', 'pid': 123,
                    'time': '2000-01-01T00:00:00+00:00'})
        with patch.object(port, 'pids', return_value=[123]), patch.object(port, 'uci_get', return_value=''):
            with patch.object(port, 'command', return_value=MagicMock(returncode=1)):
                self.assertFalse(api.status()['running'])
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                port.atomic(port.RUNTIME / 'status.json', {'state': 'running', 'pid': 123, 'time': now})
                self.assertTrue(api.status()['running'])

    def test_trial_is_bounded_and_does_not_pass_arbitrary_args(self):
        job = self.job('trial')
        with patch.object(api, 'run_child') as run:
            self.assertEqual(api.worker(job['id']), 0)
        self.assertEqual(run.call_args.args[1], [api.CLI, 'try', '--seconds', '90'])

    def test_cancelled_baseline_does_not_leave_success_receipt(self):
        job = self.job()
        (api.JOBS / ('cancel.' + job['id'])).touch()
        (port.PERSIST / 'baseline.json').write_text('{}')
        (port.RUNTIME / 'auto-baseline.json').write_text('{}')
        with patch.object(api, 'run_child'), patch.object(api, 'stop_service') as stop:
            self.assertEqual(api.worker(job['id']), 0)
        stop.assert_called_once()
        self.assertFalse((port.PERSIST / 'baseline.json').exists())
        self.assertEqual(api.optional(api.JOBS / 'job.json')['state'], 'cancelled')

    def test_failure_is_visible_in_job_status(self):
        job = self.job('repair')
        with patch.object(api, 'run_child', side_effect=RuntimeError('adapter unavailable')):
            self.assertEqual(api.worker(job['id']), 1)
        saved = api.optional(api.JOBS / 'job.json')
        self.assertEqual(saved['state'], 'failed')
        self.assertIn('adapter unavailable', saved['error'])

    def test_service_cannot_start_without_valid_baseline(self):
        job = self.job('start')
        with patch.object(port, 'Settings') as settings, patch.object(port, 'verify_baseline', side_effect=RuntimeError('baseline missing')):
            with patch.object(api, 'checked') as checked, patch.object(api, 'run_child') as run:
                self.assertEqual(api.worker(job['id']), 1)
                checked.assert_not_called()
                run.assert_not_called()

    def test_tail_is_bounded(self):
        log = api.JOBS / 'job.log'
        log.write_text('a' * 50000)
        self.assertEqual(len(api.tail(log)), 12000)

    def test_stop_waits_for_procd_controller_exit_before_repair(self):
        events = []
        with patch.object(api, 'run_child', side_effect=lambda _id, argv: events.append(argv)):
            with patch.object(api, 'controller_idle', side_effect=[False, True]):
                with patch.object(port, 'pids', return_value=[]), patch.object(api.time, 'sleep') as pause:
                    api.stop_service('a' * 32)
        self.assertEqual(events, [[api.SERVICE, 'stop'], [api.CLI, 'repair']])
        pause.assert_called_once()

    def test_rpc_acl_has_no_arbitrary_file_or_command_access(self):
        acl = json.loads((SOURCE / 'luci/root/usr/share/rpcd/acl.d/luci-app-portal-bitrate.json').read_text())
        rights = acl['luci-app-portal-bitrate']
        self.assertEqual(rights['read'], {'ubus': {'portal-bitrate': ['status']}})
        self.assertEqual(set(rights['write']), {'ubus'})
        self.assertEqual(set(rights['write']['ubus']['portal-bitrate']), set(api.METHODS) - {'status'})


if __name__ == '__main__':
    unittest.main()
