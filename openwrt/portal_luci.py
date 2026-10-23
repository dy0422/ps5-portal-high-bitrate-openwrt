#!/usr/bin/env python3
"""Narrow rpcd API and asynchronous LuCI jobs; no caller-supplied commands/paths."""
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import portal_openwrt as port

JOBS = port.RUNTIME / 'luci'
SERVICE = '/etc/init.d/portal-bitrate'
CLI = '/usr/sbin/portal-bitrate'
METHODS = {'status': {}, 'configure': {'bridge': '', 'address': '', 'ps5_ip': '', 'portal_ip': '', 'profile': ''},
           'baseline': {}, 'trial': {}, 'start': {'autostart': 0}, 'stop': {}, 'repair': {}, 'doctor': {}}


def optional(path, default=None):
    try:
        return port.read_json(path)
    except (OSError, ValueError):
        return {} if default is None else default


def process_identity(pid):
    try:
        fields = Path('/proc', str(int(pid)), 'stat').read_text().rpartition(')')[2].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, ValueError, IndexError):
        return None


def alive(pid, identity):
    return bool(pid and identity and process_identity(pid) == identity)


@contextmanager
def control_lock():
    JOBS.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (JOBS / 'control.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def current_job():
    job = optional(JOBS / 'job.json')
    job['busy'] = alive(job.get('pid'), job.get('identity'))
    job['stopping'] = bool(job.get('id') and (JOBS / ('cancel.' + job['id'])).exists())
    if not job['busy'] and job.get('state') in ('queued', 'running'):
        job['state'] = 'failed'
        job['error'] = '后台任务意外结束，请停止服务并恢复网络。'
    return job


def update_job(job_id, **values):
    with control_lock():
        job = optional(JOBS / 'job.json')
        if job.get('id') != job_id:
            raise RuntimeError('Job ownership changed')
        job.update(values)
        port.atomic(JOBS / 'job.json', job)


def tail(path, limit=12000):
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''


def status():
    job = current_job()
    report = optional(port.RUNTIME / 'status.json')
    peers = port.pids()
    try:
        stamp = datetime.datetime.fromisoformat(report['time'])
        age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
        running = (report.get('state') == 'running' and report.get('pid') in peers and 0 <= age < 15)
    except (KeyError, ValueError, TypeError):
        running = False
    config = {key: port.uci_get(key, '65' if key == 'profile' else '') for key in port.Settings.__dataclass_fields__}
    return {'ok': True, 'config': config, 'configured': (port.PERSIST / 'auto-config.json').exists(),
            'bridges': sorted(p.parent.name for p in port.SYSNET.glob('*/bridge') if p.is_dir()),
            'autostart': port.uci_get('enabled', '0') == '1' and port.command(SERVICE, 'enabled', check=False).returncode == 0,
            'running': running, 'mode': report.get('mode'),
            'recovery_required': (port.RUNTIME / 'recovery-required').exists() and not running,
            'baseline_saved': (port.PERSIST / 'baseline.json').exists(),
            'modified': report.get('counts', {}).get('unique_modified_startups', 0),
            'last_modified': report.get('last_mutation'), 'job': job,
            'log': tail(JOBS / 'job.log')}


def validate(method, request):
    if method not in METHODS:
        raise ValueError('Unknown method')
    request = {k: v for k, v in request.items() if k != 'ubus_rpc_session'}
    if set(request) != set(METHODS[method]):
        raise ValueError('Invalid arguments')
    if method == 'configure':
        if any(not isinstance(v, str) or len(v) > 80 for v in request.values()):
            raise ValueError('Invalid setting value')
        return port.asdict(port.Settings(**request).validate())
    if method == 'start' and (type(request['autostart']) is not int or request['autostart'] not in (0, 1)):
        raise ValueError('Invalid autostart setting')
    return request


def dispatch(method, request):
    request = validate(method, request)
    if method == 'status':
        return status()
    with control_lock():
        old = current_job()
        if old.get('busy'):
            if method != 'stop':
                raise RuntimeError('另一项操作正在进行，请等待完成或点击停止。')
            (JOBS / ('cancel.' + old['id'])).touch(mode=0o600)
            return {'ok': True, 'id': old['id'], 'stopping': True}
        # An orphaned foreground trial can outlive its worker. Retain its identity
        # for the stop job, so recovery can signal only the exact managed process.
        child_alive = alive(old.get('child_pid'), old.get('child_identity'))
        if child_alive and method != 'stop':
            raise RuntimeError('上一次操作仍在运行，请先点击停止。')
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'action': method, 'request': request, 'state': 'queued',
               'started': time.time()}
        if child_alive:
            job['orphan'] = {'pid': old['child_pid'], 'identity': old['child_identity'], 'action': old['action']}
        port.atomic(JOBS / 'job.json', job)
        with (JOBS / 'job.log').open('w') as output:
            worker = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), 'worker', job_id],
                                      stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                      start_new_session=True, close_fds=True)
        job.update(pid=worker.pid, identity=process_identity(worker.pid))
        port.atomic(JOBS / 'job.json', job)
        return {'ok': True, 'id': job_id}


def cancelled(job_id):
    return (JOBS / ('cancel.' + job_id)).exists()


def run_child(job_id, argv, cancellable=False):
    child = subprocess.Popen(argv)
    update_job(job_id, child_pid=child.pid, child_identity=process_identity(child.pid))
    signalled = False
    while child.poll() is None:
        if cancellable and cancelled(job_id) and not signalled:
            child.send_signal(signal.SIGTERM)
            signalled = True
        time.sleep(.2)
    update_job(job_id, child_pid=None, child_identity=None)
    if child.returncode != 0 and not signalled:
        raise RuntimeError('操作失败，请查看下方日志。')


def checked(*args):
    result = port.command(*args, check=False)
    if result.stdout:
        print(result.stdout, end='', flush=True)
    if result.returncode:
        if result.stderr:
            print(result.stderr, end='', flush=True)
        raise RuntimeError('命令执行失败：' + str(args[0]))


def stop_service(job_id, orphan=None):
    if orphan and alive(orphan['pid'], orphan['identity']):
        # Never interrupt enrollment midway through its namespace setup.
        if orphan.get('action') in ('baseline', 'trial', 'doctor'):
            os.kill(orphan['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 90
        while alive(orphan['pid'], orphan['identity']) and time.monotonic() < deadline:
            time.sleep(.25)
        if alive(orphan['pid'], orphan['identity']):
            raise RuntimeError('上一次操作仍未结束，已保留恢复路径，请稍后重试。')
    run_child(job_id, [SERVICE, 'stop'])
    # procd acknowledges its stop request before the supervised process finishes.
    # Wait for the controller's lock and namespace processes before ARP repair.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if controller_idle() and not port.pids():
            break
        time.sleep(.25)
    else:
        raise RuntimeError('服务仍在退出，已保留恢复路径，请稍后重试停止。')
    run_child(job_id, [CLI, 'repair'])


def controller_idle():
    try:
        with port.exclusive():
            return True
    except RuntimeError:
        return False


def worker(job_id):
    with control_lock():
        job = optional(JOBS / 'job.json')
        if job.get('id') != job_id:
            raise RuntimeError('Unknown job')
        job.update(pid=os.getpid(), identity=process_identity(os.getpid()), state='running')
        port.atomic(JOBS / 'job.json', job)
    action, request = job['action'], validate(job['action'], job['request'])
    try:
        if action == 'configure':
            print('正在保存设置并检测 PS5 / Portal，请保持两台设备开机。', flush=True)
            run_child(job_id, [CLI, 'configure', '--bridge', request['bridge'], '--address', request['address'],
                              '--ps5', request['ps5_ip'], '--portal', request['portal_ip'], '--profile', request['profile']])
            checked(SERVICE, 'disable')
        elif action in ('baseline', 'trial', 'doctor', 'repair'):
            if action == 'baseline':
                print('正在准备连接测试，请等待就绪提示再连接 Portal。', flush=True)
            if action == 'trial':
                print('正在准备试运行，请等待就绪提示再重新连接 Portal。', flush=True)
            argv = [CLI, 'try', '--seconds', '90'] if action == 'trial' else [CLI, action]
            run_child(job_id, argv, cancellable=action in ('baseline', 'trial', 'doctor'))
        elif action == 'start':
            port.verify_baseline(port.Settings.load())
            checked('uci', 'set', 'portal_bitrate.main.enabled=1')
            checked('uci', 'commit', 'portal_bitrate')
            checked(SERVICE, 'enable' if request['autostart'] else 'disable')
            run_child(job_id, [SERVICE, 'start'])
            deadline = time.monotonic() + 25
            while not cancelled(job_id) and time.monotonic() < deadline:
                if status()['running']:
                    print('服务已启动，请在 Portal 上重新连接并检查实际画面与码率。', flush=True)
                    break
                time.sleep(.5)
            else:
                if not cancelled(job_id):
                    raise RuntimeError('服务未正常启动，请运行环境检查并查看日志。')
        elif action == 'stop':
            stop_service(job_id, job.get('orphan'))
        if cancelled(job_id):
            stop_service(job_id)
            if action == 'baseline':
                (port.PERSIST / 'baseline.json').unlink(missing_ok=True)
                (port.RUNTIME / 'auto-baseline.json').unlink(missing_ok=True)
            update_job(job_id, state='cancelled', finished=time.time())
            print('已停止，网络恢复完成。', flush=True)
        else:
            update_job(job_id, state='complete', finished=time.time())
            print('操作完成。', flush=True)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        update_job(job_id, state='failed', error=str(exc), finished=time.time())
        return 1
    finally:
        (JOBS / ('cancel.' + job_id)).unlink(missing_ok=True)
    return 0


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('rpcd must run this plugin as root')
    if sys.argv[1:] == ['list']:
        print(json.dumps(METHODS))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == 'worker':
        if len(sys.argv[2]) != 32 or any(c not in '0123456789abcdef' for c in sys.argv[2]):
            raise ValueError('Invalid job ID')
        return worker(sys.argv[2])
    if len(sys.argv) != 3 or sys.argv[1] != 'call':
        raise ValueError('Invalid rpcd invocation')
    try:
        request = json.loads(sys.stdin.read(8193) or '{}')
        if not isinstance(request, dict):
            raise ValueError('Invalid request')
        print(json.dumps(dispatch(sys.argv[2], request), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
