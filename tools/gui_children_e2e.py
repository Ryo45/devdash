#!/usr/bin/env python3
"""Opt-in GPUI -> Core -> supervisord test, only a dedicated Docker fixture.
Build tests/fixtures/supervisor/Dockerfile first. Requires Xephyr + Pillow.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import threading
import time
from gui_e2e import ROOT, Scenario, RecordingProxy, stop, wait_until

OUT=ROOT/'target/gui-children-e2e'
def docker(*args,check=True):
    return subprocess.run(['docker',*args],text=True,capture_output=True,check=check)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--image',default='devdash-supervisor-test:4.3.0')
    args=parser.parse_args()
    if not args.run: parser.error('pass --run to create/operate a dedicated test container')
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'result.json').unlink(missing_ok=True)
    name=f'devdash-gui-child-{os.getpid()}'
    assert re.fullmatch(r'devdash-gui-child-\d+',name)
    docker('create','--name',name,'--network','none','--cpus','1','--memory','512m',args.image)
    result=None
    try:
        with tempfile.TemporaryDirectory(prefix='devdash-gui-child-') as temporary, ExitStack() as stack:
            temp=Path(temporary)
            config={'workloads':{'box':{'name':'Docker lab','backend':'docker','docker':{'container':name},
                      'controller':{'kind':'supervisord','socket':'/tmp/devdash-supervisor.sock'}}},'groups':{}}
            path=temp/'config.yaml';path.write_text(json.dumps(config))
            nested_log=stack.enter_context((OUT/'xephyr.log').open('w'))
            nested=subprocess.Popen(['Xephyr','-displayfd','1','-screen','1440x1000','-nolisten','tcp','-noreset','-ac'],
                                    stdout=subprocess.PIPE,stderr=nested_log,text=True)
            stack.callback(stop,nested)
            assert select.select([nested.stdout],[],[],15)[0]
            display=nested.stdout.readline().strip();assert display.isdigit()
            os.environ['DISPLAY']=':'+display;os.environ.pop('WAYLAND_DISPLAY',None)
            icd=Path('/usr/share/vulkan/icd.d/lvp_icd.json')
            if icd.exists():os.environ['VK_ICD_FILENAMES']=str(icd)
            core=temp/'core.sock';proxy_path=temp/'gui.sock'
            log=stack.enter_context((OUT/'core.log').open('w'))
            daemon=subprocess.Popen([str(ROOT/'target/debug/devdash'),'--socket',str(core),'daemon','--config',str(path),'--state',str(temp/'state.json')],stdout=log,stderr=subprocess.STDOUT)
            stack.callback(stop,daemon);wait_until('Core',core.exists)
            proxy=RecordingProxy(proxy_path,core)
            threading.Thread(target=proxy.serve_forever,daemon=True).start()
            stack.callback(proxy.server_close);stack.callback(proxy.shutdown)
            gui_log=stack.enter_context((OUT/'gui.log').open('w'))
            gui=subprocess.Popen([str(ROOT/'target/debug/devdash-gui'),'--socket',str(proxy_path)],stdout=gui_log,stderr=subprocess.STDOUT)
            stack.callback(stop,gui)
            def find():
                assert gui.poll() is None,'GUI exited'
                matches=re.findall(r'(0x[0-9a-f]+) "devdash"',subprocess.check_output(['xwininfo','-root','-tree'],text=True))
                return matches[-1] if matches else None
            window=wait_until('GPUI window',find,25);time.sleep(1.5)
            scenario=Scenario(core,proxy,window,output=OUT)
            def failure(exc_type,error,traceback):
                if exc_type is not None:
                    try:
                        scenario.image().save(OUT/'failure.png')
                        (OUT/'failure-state.json').write_text(json.dumps(scenario.snapshot(),indent=2))
                        (OUT/'failure.txt').write_text(repr(error))
                    except Exception:pass
                return False
            stack.push(failure)
            scenario.select(0)
            scenario.command('start','box',False);scenario.finished('box')
            state=scenario.wait_snapshot('supervisord ready',lambda s:s['observations']['box']['health']=='healthy'
                and any(c['id']=='worker:worker' and c['state']=='RUNNING' for c in s['observations']['box']['children']))
            epoch=next(c['epoch'] for c in state['observations']['box']['children'] if c['id']=='worker:worker')
            scenario.capture('01-container-start')
            scenario.select(2)  # box -> idle:idle -> worker:worker, stable logical services.
            marker=len(proxy.recorded());scenario.at(318,scenario.panel_top()+60);time.sleep(.7)
            assert not any(e['request']['command']=='operate_child' for e in proxy.recorded()[marker:]),'Start must be disabled for RUNNING service'
            scenario.child_command('stop','box','worker:worker');scenario.finished('box/worker:worker')
            scenario.wait_snapshot('worker stopped',lambda s:any(c['id']=='worker:worker' and c['state']=='STOPPED' for c in s['observations']['box']['children']))
            assert scenario.snapshot()['observations']['box']['health']=='healthy'
            scenario.capture('02-stopped-service')
            scenario.child_command('start','box','worker:worker');scenario.finished('box/worker:worker')
            running=scenario.wait_snapshot('worker started',lambda s:any(c['id']=='worker:worker' and c['state']=='RUNNING' for c in s['observations']['box']['children']))
            pid=next(c['pid'] for c in running['observations']['box']['children'] if c['id']=='worker:worker')
            scenario.child_command('restart','box','worker:worker');scenario.finished('box/worker:worker')
            scenario.wait_snapshot('worker new PID',lambda s:any(c['id']=='worker:worker' and c['pid'] not in (None,pid) for c in s['observations']['box']['children']))
            scenario.capture('03-child-restart')
            wait_until('container logs received by GUI',lambda:any(e['request'].get('command')=='logs' and 'devdash isolated supervisor worker ready' in str(e['response']['data']) for e in proxy.recorded()))
            # A controller/container incarnation change must clear a selected
            # child, so a stale click cannot silently turn into a parent Stop.
            docker('restart',name)
            scenario.wait_snapshot('external incarnation change',lambda s:any(c['id']=='worker:worker' and c['epoch']!=epoch and c['state']=='RUNNING' for c in s['observations']['box']['children']))
            scenario.sync()
            marker=len(proxy.recorded());scenario.at(378,scenario.panel_top()+60);time.sleep(.6)
            assert not any(e['request']['command'] in ('operate','operate_child') for e in proxy.recorded()[marker:]),'stale child selection must clear instead of targeting the parent'
            scenario.capture('04-external-incarnation')
            epoch=next(c['epoch'] for c in scenario.snapshot()['observations']['box']['children'] if c['id']=='worker:worker')
            scenario.select(0,folder=True)
            scenario.command('restart','box',False);scenario.finished('box')
            scenario.wait_snapshot('new container incarnation',lambda s:any(c['id']=='worker:worker' and c['epoch']!=epoch and c['state']=='RUNNING' for c in s['observations']['box']['children']))
            scenario.capture('05-container-restart')
            mutations=[e['request'] for e in proxy.recorded() if e['request']['command'] in ('operate','operate_child')]
            assert len(mutations)==5,mutations
            (OUT/'traffic.json').write_text(json.dumps([e for e in proxy.recorded() if e['request']['command']!='snapshot'],indent=2))
            result={'passed':scenario.steps,'mutations':mutations,'container':name}
    finally:
        removed=docker('rm','-f',name,check=False)
        (OUT/'cleanup.log').write_text(removed.stdout+removed.stderr)
        assert removed.returncode==0,'dedicated container cleanup failed'
        if result is not None:
            result['cleanup_complete']=True
            (OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()
