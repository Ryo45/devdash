#!/usr/bin/env python3
"""GPUI -> Unix IPC -> real systemd end-to-end test, explicit opt-in.

Uses only uniquely named runtime dummy user services and an isolated Xephyr.
Mutating devdash requests come exclusively from mouse clicks. Read-only IPC
assertions and a recording proxy verify what the GUI sent and received.
Artifacts: target/gui-e2e/{result.json,traffic.json,*.png,*.log}.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from PIL import ImageGrab
from gui_smoke import click, geometry

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'target' / 'gui-e2e'


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_until(description, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(f'timed out: {description}')


def request(path, body, check=True):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(json.dumps(body).encode() + b'\n')
        with client.makefile('rb') as stream:
            response = json.loads(stream.readline())
        if check:
            assert response['ok'], response.get('error')
        return response


class RecordingProxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, address, core):
        self.core = core
        self.traffic = []
        self.lock = threading.Lock()
        super().__init__(str(address), ProxyRequest)

    def recorded(self):
        with self.lock:
            return list(self.traffic)


class ProxyRequest(socketserver.StreamRequestHandler):
    def handle(self):
        body = json.loads(self.rfile.readline())
        response = request(self.server.core, body, check=False)
        with self.server.lock:
            self.server.traffic.append({'request': body, 'response': response})
        self.wfile.write(json.dumps(response).encode() + b'\n')


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], text=True,
                          capture_output=True, check=check)


class Scenario:
    def __init__(self, core, proxy, window, output=None):
        self.core, self.proxy, self.window = core, proxy, window
        self.output = output or OUT
        self.steps = []

    def snapshot(self):
        return request(self.core, {'command': 'snapshot'})['data']

    def wait_snapshot(self, description, predicate):
        return wait_until(description, lambda: (s if predicate(s) else None)
                          if (s := self.snapshot()) else None)

    def image(self):
        x, y, width, height = geometry(self.window)
        return ImageGrab.grab(bbox=(x, y, x + width, y + height))

    def panel_top(self):
        # Follow layout shifts caused by warning banners, without OCR or a
        # production-only test API. Theme/layout changes should fail this test.
        image = self.image()
        return next(y for y in range(85, 260)
                    if all(image.getpixel((20, y + dy))[:3] == (24, 30, 40)
                           for dy in range(10)))

    def at(self, x, y):
        left, top, _, _ = geometry(self.window)
        click(left + x, top + y)
        time.sleep(.25)

    def select(self, row, folder=False):
        for _ in range(2 if folder else 1):
            self.at(115, self.panel_top() + 24 + row * 34)
        time.sleep(.3)

    def command(self, name, target=None, group=None, button=None):
        self.sync()
        previous = len(self.proxy.recorded())
        positions = {'start': 318, 'stop': 378, 'restart': 447, 'reinitialize': 535}
        if button == 'approve':
            self.at(610, self.panel_top() + 150)
        elif button == 'cancel':
            self.at(1200, 32)
        else:
            self.at(positions[name], self.panel_top() + 60)
        def sent():
            for entry in self.proxy.recorded()[previous:]:
                body = entry['request']
                if name in positions:
                    matches = (body.get('command') == 'operate' and body.get('action') == name
                               and body.get('target') == target and body.get('group') == group)
                else:
                    matches = body.get('command') == name
                if matches:
                    assert entry['response']['ok']
                    return entry
        return wait_until(f'GUI sent {name} {target}', sent, timeout=4)

    def child_command(self, action, parent, child):
        self.sync()
        marker = len(self.proxy.recorded())
        self.at({'start':318,'stop':378,'restart':447}[action], self.panel_top()+60)
        return wait_until('GUI child command', lambda: next((entry for entry in self.proxy.recorded()[marker:]
            if entry['request'].get('command')=='operate_child' and entry['request'].get('parent')==parent
            and entry['request'].get('child')==child and entry['request'].get('action')==action), None), timeout=4)

    def finished(self, target, status='completed'):
        return self.wait_snapshot(f'{target} {status}',
                                  lambda s: s['run']['target'] == target and s['run']['status'] == status)

    def sync(self):
        # Wait until the GUI actually receives the asserted run/state snapshot.
        marker = len(self.proxy.recorded())
        def logical_children(observation):
            return [(c['id'],c['epoch'],c['state'],c['pid'],c['actions'],c.get('runtime_id'))
                    for c in observation.get('children',[]) if c['kind']!='process']
        def delivered():
            expected = self.snapshot()
            for entry in self.proxy.recorded()[marker:]:
                if entry['request']['command'] != 'snapshot':
                    continue
                data = entry['response']['data']
                if data['run'] == expected['run'] and all(
                    data['observations'][key]['state'] == value['state']
                    and data['observations'][key]['pid'] == value['pid']
                    and logical_children(data['observations'][key]) == logical_children(value)
                    for key, value in expected['observations'].items()):
                    return True
        try:
            wait_until('GUI received current snapshot', delivered, timeout=6)
        except Exception:
            self.image().save(self.output / 'failure.png')
            (self.output / 'failure-state.json').write_text(json.dumps(self.snapshot(), indent=2))
            raise
        time.sleep(.3)  # The UI drains worker updates every 150ms.


    def capture(self, name):
        self.sync()
        self.image().save(self.output / f'{name}.png')
        (self.output / f'{name}.json').write_text(json.dumps(self.snapshot(), indent=2) + '\n')
        self.steps.append(name)
        print(f'PASS {name}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='opt in to dedicated dummy user units')
    args = parser.parse_args()
    if not args.run:
        parser.error('pass --run to create and clean up dedicated dummy user units')
    OUT.mkdir(parents=True, exist_ok=True)
    for artifact in ('result.json', 'failure.txt', 'failure.png', 'failure-state.json'):
        (OUT / artifact).unlink(missing_ok=True)
    prefix = f'devdash-gui-it-{os.getpid()}-{time.time_ns()}'
    names = [f'{prefix}-{name}.service' for name in ('camera', 'imu', 'detector', 'planner', 'failure')]
    assert all(re.fullmatch(r'devdash-gui-it-\d+-\d+-\w+\.service', name) for name in names)
    registered = False
    scenario = proxy = result = None
    temporary_dir = tempfile.TemporaryDirectory(prefix='devdash-gui-e2e-')
    try:
        with ExitStack() as stack:
            temp = Path(temporary_dir.name)
            files = []
            workloads = {}
            for workload, name in zip(('camera', 'imu', 'detector', 'planner', 'failure'), names):
                path = temp / name
                command = '/usr/bin/false' if workload == 'failure' else (
                    f"/bin/sh -c 'echo {prefix}-{workload}; exec /usr/bin/sleep infinity'")
                kind = 'oneshot' if workload == 'failure' else 'simple'
                path.write_text(f'[Unit]\nDescription=devdash isolated GUI test\n[Service]\nType={kind}\nExecStart={command}\nKillMode=control-group\n')
                files.append(str(path))
                workloads[workload] = {'backend': 'systemd', 'scope': 'user', 'unit': name}
            config = {'workloads': workloads, 'groups': {
                'robot': {'name': 'Robot', 'steps': [{'group': 'sensors'}, {'gate': 'sensors_ready'},
                                                    {'group': 'perception'}, {'group': 'planning'}]},
                'sensors': {'steps': [{'parallel': ['camera', 'imu']}]},
                'perception': {'steps': [{'workload': 'detector'}]},
                'planning': {'steps': [{'workload': 'planner'}]},
            }, 'gates': {'sensors_ready': {'type': 'manual'}}}
            config_path = temp / 'config.yaml'
            config_path.write_text(json.dumps(config, indent=2))  # JSON is valid YAML.
            registered = True  # Clean even a partially successful link operation.
            systemctl('link', '--runtime', *files)
            systemctl('daemon-reload')
            nested_log = stack.enter_context((OUT / 'xephyr.log').open('w'))
            nested = subprocess.Popen(['Xephyr', '-displayfd', '1', '-screen', '1440x1000',
                                       '-nolisten', 'tcp', '-noreset', '-ac'],
                                      stdout=subprocess.PIPE, stderr=nested_log, text=True)
            stack.callback(stop, nested)
            assert select.select([nested.stdout], [], [], 15)[0], 'Xephyr startup timeout'
            display = nested.stdout.readline().strip()
            assert display.isdigit(), 'Xephyr failed'
            os.environ['DISPLAY'] = ':' + display
            os.environ.pop('WAYLAND_DISPLAY', None)
            icd = Path('/usr/share/vulkan/icd.d/lvp_icd.json')
            if icd.exists():
                os.environ['VK_ICD_FILENAMES'] = str(icd)
            core = temp / 'core.sock'
            daemon_log = stack.enter_context((OUT / 'core.log').open('w'))
            daemon = subprocess.Popen([str(ROOT / 'target/debug/devdash'), '--socket', str(core),
                                       'daemon', '--config', str(config_path), '--state', str(temp / 'state.json')],
                                      stdout=daemon_log, stderr=subprocess.STDOUT)
            stack.callback(stop, daemon)
            wait_until('Core socket', core.exists)
            # The GUI uses this proxy; assertions read the actual Core directly.
            proxy_socket = temp / 'gui.sock'
            proxy = RecordingProxy(proxy_socket, core)
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            stack.callback(proxy.server_close)
            stack.callback(proxy.shutdown)
            gui_log = stack.enter_context((OUT / 'gui.log').open('w'))
            gui_processes = []
            def launch_gui():
                gui = subprocess.Popen([str(ROOT / 'target/debug/devdash-gui'), '--socket', str(proxy_socket)],
                                       stdout=gui_log, stderr=subprocess.STDOUT)
                gui_processes.append(gui)
                stack.callback(stop, gui)
                def find():
                    assert gui.poll() is None, 'GUI exited'
                    matches = re.findall(r'(0x[0-9a-f]+) "devdash"',
                                         subprocess.check_output(['xwininfo', '-root', '-tree'], text=True))
                    return matches[-1] if matches else None
                window = wait_until('GPUI window', find, timeout=25)
                time.sleep(1.5)
                return gui, window
            gui, window = launch_gui()
            scenario = Scenario(core, proxy, window)
            def capture_failure(exc_type, _error, _traceback):
                if exc_type is not None and not (OUT / 'failure.png').exists():
                    try:
                        scenario.image().save(OUT / 'failure.png')
                        (OUT / 'failure-state.json').write_text(json.dumps(scenario.snapshot(), indent=2))
                    except Exception:
                        pass  # Artifact capture must not mask the original failure.
                return False
            stack.push(capture_failure)
            scenario.wait_snapshot('initial states', lambda s: all(
                s['observations'][name]['state'] == 'inactive' for name in workloads))
            scenario.select(0, folder=True)
            scenario.command('start', 'robot', True)
            gate = scenario.wait_snapshot('manual gate with sensors active', lambda s: s['run']['waiting_gate'] == 'sensors_ready'
                and all(s['observations'][name]['state'] == 'active' for name in ('camera', 'imu')))
            assert gate['observations']['detector']['state'] != 'active'
            assert 'sensors_ready' not in gate['approved']
            scenario.capture('01-gate-wait')
            # Closing the GUI leaves the headless sequence waiting.
            stop(gui)
            assert scenario.snapshot()['run']['waiting_gate'] == 'sensors_ready'
            gui, scenario.window = launch_gui()
            stack.push(capture_failure)
            scenario.select(0, folder=True)
            scenario.command('approve', button='approve')
            scenario.finished('robot')
            started = scenario.wait_snapshot('all active', lambda s: all(
                s['observations'][name]['state'] == 'active' for name in ('camera', 'imu', 'detector', 'planner')))
            assert 'sensors_ready' in started['approved']
            sensor_pids = {name: started['observations'][name]['pid'] for name in ('camera', 'imu')}
            assert all(sensor_pids.values())
            detector_pid = started['observations']['detector']['pid']
            scenario.capture('02-approved-start')
            scenario.select(5, folder=True)
            scenario.command('restart', 'perception', True)
            scenario.finished('perception')
            partial = scenario.wait_snapshot('detector restarted', lambda s:
                s['observations']['detector']['pid'] not in (None, detector_pid))
            assert all(partial['observations'][name]['pid'] == pid for name, pid in sensor_pids.items())
            scenario.capture('03-partial-restart')
            scenario.select(0, folder=True)
            scenario.command('restart', 'robot', True)
            restarted = scenario.finished('robot')
            assert 'sensors_ready' in restarted['approved'] and restarted['run']['waiting_gate'] is None
            completed = [p for p in restarted['run']['progress'] if p['status'] == 'completed' and p['step'] in workloads]
            assert completed[0]['step'] == 'planner' and completed[0]['action'] == 'stop'
            assert completed[-1]['step'] == 'planner' and completed[-1]['action'] == 'start'
            scenario.capture('04-restart-reuses-approval')
            scenario.command('reinitialize', 'robot', True)
            scenario.wait_snapshot('reinitialized gate', lambda s: s['run']['waiting_gate'] == 'sensors_ready'
                                   and 'sensors_ready' not in s['approved'])
            scenario.command('cancel', button='cancel')
            cancelled = scenario.finished('robot', 'cancelled')
            assert any(p['step'] == 'detector' and p['status'] == 'skipped' for p in cancelled['run']['progress'])
            state = scenario.wait_snapshot('cancel preserves sensors', lambda s:
                s['observations']['camera']['state'] == 'active' and s['observations']['detector']['state'] == 'inactive')
            sensor_pid = state['observations']['camera']['pid']
            scenario.capture('05-cancel-no-rollback')
            # Individual operations bypass gates and trigger an observed mismatch.
            scenario.select(6)
            scenario.command('start', 'detector', False)
            scenario.finished('detector')
            scenario.wait_snapshot('gate mismatch warning', lambda s: any(
                event['kind'] == 'gate_mismatch' for event in s['events']))
            assert scenario.snapshot()['observations']['camera']['pid'] == sensor_pid
            wait_until('GUI journal response', lambda: any(
                item['request'] == {'command': 'logs', 'workload': 'detector'}
                and f'{prefix}-detector' in str(item['response']['data']) for item in proxy.recorded()))
            scenario.capture('06-individual-start-and-warning')
            previous_pid = scenario.snapshot()['observations']['detector']['pid']
            scenario.command('restart', 'detector', False)
            single = scenario.finished('detector')
            assert len(single['run']['progress']) == 1
            scenario.wait_snapshot('individual restart changed PID', lambda s:
                s['observations']['detector']['pid'] not in (None, previous_pid))
            scenario.capture('07-individual-restart')
            scenario.select(9)
            scenario.command('start', 'failure', False)
            failed = scenario.finished('failure', 'failed')
            assert any('job result' in str(p['error']) for p in failed['run']['progress'])
            assert scenario.snapshot()['observations']['camera']['pid'] == sensor_pid
            scenario.capture('08-job-error')
            scenario.select(0, folder=True)
            scenario.command('stop', 'robot', True)
            scenario.finished('robot')
            scenario.wait_snapshot('group stopped', lambda s: all(s['observations'][name]['state'] == 'inactive'
                                   for name in ('camera', 'imu', 'detector', 'planner')))
            scenario.capture('09-stop')
            mutations = [item['request'] for item in proxy.recorded()
                         if item['request']['command'] in ('operate', 'approve', 'cancel')]
            assert len(mutations) == 10, mutations
            result = {'passed': scenario.steps, 'gui_mutations': mutations,
                      'dedicated_units': names, 'core_survived_gui_close': True}

    except BaseException as error:
        (OUT / 'failure.txt').write_text(repr(error) + '\n')
        if scenario is not None:
            try:
                scenario.image().save(OUT / 'failure.png')
                (OUT / 'failure-state.json').write_text(json.dumps(scenario.snapshot(), indent=2))
            except Exception:
                pass
        raise
    finally:
        if proxy is not None:
            # Snapshot polling traffic is verbose; keep commands and their results.
            records = [entry for entry in proxy.recorded() if entry['request']['command'] != 'snapshot']
            (OUT / 'traffic.json').write_text(json.dumps(records, indent=2) + '\n')
        if registered:
            cleanup = [systemctl('stop', *names, check=False)]
            for name in names:
                state = systemctl('show', name, '--property=ActiveState', '--value', check=False)
                cleanup.append(state)
                if state.stdout.strip() == 'failed':
                    cleanup.append(systemctl('reset-failed', name, check=False))
            cleanup.extend([systemctl('disable', '--runtime', *names, check=False),
                            systemctl('daemon-reload', check=False)])
            (OUT / 'cleanup.log').write_text('\n'.join(item.stdout + item.stderr for item in cleanup))
            assert all(item.returncode == 0 for item in cleanup), 'dummy-unit cleanup failed; see cleanup.log'
            assert all(systemctl('show', name, '--property=ActiveState', '--value').stdout.strip() == 'inactive'
                       for name in names), 'dummy service still active'
            assert all(systemctl('show', name, '--property=LoadState', '--value').stdout.strip() == 'not-found'
                       for name in names), 'dummy unit still registered'
        if result is not None:
            result['cleanup_complete'] = True
            (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
        temporary_dir.cleanup()


if __name__ == '__main__':
    main()
