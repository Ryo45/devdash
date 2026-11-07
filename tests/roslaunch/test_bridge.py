#!/usr/bin/env python3
"""Actual ROS 2 Jazzy integration, in an isolated ROS container/session."""
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]

def rpc(path, request):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(20)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b'\n')
        with client.makefile('rb') as reader:
            return json.loads(reader.readline())

def until(description, function, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('timed out: ' + description)

with tempfile.TemporaryDirectory(prefix='devdash-ros-bridge-') as temporary:
    path = Path(temporary) / 'bridge.sock'
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'bridges/roslaunch') + os.pathsep + env.get('PYTHONPATH','')
    log = open(Path(temporary) / 'bridge.log', 'w+')
    wrapper = subprocess.Popen([sys.executable, '-m', 'devdash_roslaunch', '--socket', str(path),
                               str(ROOT / 'tests/fixtures/roscomponents/robot.launch.py')], env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    def snapshot():
        assert wrapper.poll() is None, 'wrapper exited'
        response = rpc(path, {'command':'snapshot'})
        assert response['ok'], response
        return response['data']
    def find(predicate):
        return next((c for c in snapshot()['children'] if predicate(c)), None)
    def operate(child, action, ok=True):
        response = rpc(path, {'command':'operate','id':child['id'],'epoch':child['epoch'],'action':action})
        assert response['ok'] == ok, response
        return response
    try:
        until('bridge socket', path.exists)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        managed = until('managed loaded component', lambda: find(lambda c:c['kind']=='component' and c['id'].endswith(':probe') and c['state']=='loaded' and 'restart' in c['actions']))
        ordinary = until('native component', lambda: find(lambda c:c['kind']=='component' and c['id'].startswith('component-observed:')))
        assert ordinary['actions']==['stop']
        operate(ordinary, 'restart', ok=False)
        assert managed['pid'] is None and managed['memory_bytes'] is None, 'no per-component process attribution'
        logical_id, runtime_id = managed['id'], managed['runtime_id']
        operate(managed,'stop')
        stopped=until('unloaded logical component retained',lambda: find(lambda c:c['id']==logical_id and c['state']=='unloaded' and 'start' in c['actions']))
        operate(stopped,'start')
        loaded=until('reloaded component',lambda: find(lambda c:c['id']==logical_id and c['state']=='loaded' and c['runtime_id']!=runtime_id))
        assert loaded['name']==managed['name'], 'original resolved LoadNode request must be reused'
        previous=loaded['runtime_id']
        operate(loaded,'restart')
        until('component restart assigned new runtime ID',lambda: find(lambda c:c['id']==logical_id and c['state']=='loaded' and c['runtime_id']!=previous))
        stale=dict(loaded,epoch='stale-generation')
        operate(stale,'stop',ok=False)
        operate(ordinary,'stop')
        until('native component unloaded',lambda: not find(lambda c:c['id']==ordinary['id']))
        process=until('launch process running',lambda: find(lambda c:c['kind']=='service' and 'sleep' in c['name'] and c['state']=='running'))
        assert process['actions']==['stop']
        operate(process,'restart',ok=False)
        operate(process,'stop')
        exited=until('process exit retained',lambda: find(lambda c:c['id']==process['id'] and c['state']=='exited'))
        assert exited['exit_code'] is not None and exited['pid'] is None
        time.sleep(.4)
        assert find(lambda c:c['id']==process['id'])['state']=='exited', 'no bridge restart/respawn loop'
        # An external native unload is observed, never automatically repaired.
        import rclpy
        from rclpy.node import Node
        from composition_interfaces.srv import UnloadNode
        rclpy.init()
        peer=Node('_devdash_external_component_test')
        try:
            client=peer.create_client(UnloadNode,'/devdash_test/devdash_component_manager/_container/unload_node')
            assert client.wait_for_service(timeout_sec=5)
            current=find(lambda c:c['id']==logical_id)
            unload=UnloadNode.Request();unload.unique_id=int(current['runtime_id'])
            future=client.call_async(unload)
            rclpy.spin_until_future_complete(peer,future,timeout_sec=5)
            assert future.done() and future.result().success
            stopped=until('external unload observed',lambda:find(lambda c:c['id']==logical_id and c['state']=='unloaded'))
            time.sleep(.7)
            assert find(lambda c:c['id']==logical_id)['state']=='unloaded'
            operate(stopped,'start')
        finally:
            peer.destroy_node();rclpy.shutdown()
        # Streaming notifications use the same epoch and increasing sequence.
        with socket.socket(socket.AF_UNIX) as watcher:
            watcher.settimeout(10)
            watcher.connect(str(path))
            watcher.sendall(b'{"command":"watch"}\n')
            with watcher.makefile('rb') as reader:
                first=json.loads(reader.readline())['data']
                current=find(lambda c:c['id']==logical_id)
                operate(current,'stop')
                event=json.loads(reader.readline())['data']
                assert event['epoch']==first['epoch'] and event['sequence']>first['sequence']
        print('DEVDASH_BRIDGE ' + json.dumps({'component_load_unload_reload':True,'original_request_preserved':True,
            'ordinary_component_unload_only':True,'process_stop_without_restart':True,
            'stable_logical_id':True,'stale_epoch_rejected':True,'watch_notifications':True}))
    except BaseException:
        try: print(json.dumps(snapshot(), indent=2), file=sys.stderr)
        except Exception: pass
        log.flush();log.seek(0);print(log.read(),file=sys.stderr)
        raise
    finally:
        wrapper.terminate()
        try: wrapper.wait(timeout=10)
        except subprocess.TimeoutExpired: wrapper.kill();wrapper.wait()
        log.close()
