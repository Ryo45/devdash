#!/usr/bin/env python3
"""Local X11 GUI smoke: starts only devdash; never starts existing services.
Requires Python Pillow, Xephyr and X11's xwininfo. Writes screenshots under target/.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import select
import subprocess
import tempfile
import time
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'target' / 'gui-smoke'
OUT.mkdir(parents=True, exist_ok=True)

def geometry(window):
    info = subprocess.check_output(['xwininfo', '-id', window], text=True)
    def value(label):
        return int(re.search(label + r':\s+(-?\d+)', info).group(1))
    return (value('Absolute upper-left X'), value('Absolute upper-left Y'), value('Width'), value('Height'))

def click(x,y):
    x11=ctypes.CDLL('libX11.so.6')
    xtst=ctypes.CDLL('libXtst.so.6')
    x11.XOpenDisplay.argtypes=[ctypes.c_char_p]
    x11.XOpenDisplay.restype=ctypes.c_void_p
    x11.XFlush.argtypes=[ctypes.c_void_p]
    x11.XCloseDisplay.argtypes=[ctypes.c_void_p]
    xtst.XTestFakeMotionEvent.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
    display=x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError('X11 display unavailable')
    try:
        xtst.XTestFakeMotionEvent(display,-1,x,y,0)
        xtst.XTestFakeButtonEvent(display,1,1,0)
        xtst.XTestFakeButtonEvent(display,1,0,0)
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)

def main():
    # Isolate GUI input and capture from the user's desktop, including lock screens.
    nested_log=(OUT/'xephyr.log').open('w')
    nested=subprocess.Popen(['Xephyr','-displayfd','1','-screen','1440x1000','-nolisten','tcp','-noreset','-ac'],stdout=subprocess.PIPE,stderr=nested_log,text=True)
    if not select.select([nested.stdout],[],[],15)[0]:
        nested.terminate()
        raise RuntimeError('Xephyr startup timeout')
    display_number=nested.stdout.readline().strip()
    if not display_number.isdigit():
        raise RuntimeError('Xephyr failed: see target/gui-smoke/xephyr.log')
    os.environ['DISPLAY']=':'+display_number
    os.environ.pop('WAYLAND_DISPLAY',None)
    software_icd=Path('/usr/share/vulkan/icd.d/lvp_icd.json')
    if software_icd.exists():
        os.environ['VK_ICD_FILENAMES']=str(software_icd)

    with tempfile.TemporaryDirectory(prefix='devdash-gui-') as temporary:
        socket=Path(temporary)/'core.sock'
        daemon_log=(OUT/'core.log').open('w')
        gui_log=(OUT/'gui.log').open('w')
        daemon=subprocess.Popen([str(ROOT/'target/debug/devdash'),'--socket',str(socket),'daemon','--config',str(ROOT/'examples/robot.yaml'),'--state',str(Path(temporary)/'state.json')],stdout=daemon_log,stderr=subprocess.STDOUT)
        gui=None
        try:
            deadline=time.monotonic()+15
            while not socket.exists():
                assert daemon.poll() is None, 'daemon exited'
                assert time.monotonic()<deadline,'daemon startup timed out'
                time.sleep(.1)
            gui=subprocess.Popen([str(ROOT/'target/debug/devdash-gui'),'--socket',str(socket)],stdout=gui_log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+25
            window=None
            while not window:
                assert gui.poll() is None,'GUI exited'
                windows=subprocess.check_output(['xwininfo','-root','-tree'],text=True)
                matches=re.findall(r'(0x[0-9a-f]+) "devdash"',windows)
                if matches:
                    window=matches[-1]
                assert time.monotonic()<deadline,'no devdash window'
                time.sleep(.2)
            time.sleep(2)
            x,y,w,h=geometry(window)
            click(x+115,y+122)
            time.sleep(.3)
            click(x+115,y+122)
            time.sleep(1)
            ImageGrab.grab(bbox=(x,y,x+w,y+h)).save(OUT/'main.png')
            # The top-right Scan button is independent of Group/service selection.
            click(x+w-305,y+32)
            time.sleep(2)
            ImageGrab.grab(bbox=(x,y,x+w,y+h)).save(OUT/'scan-user.png')
            click(x+w-205,y+32)
            time.sleep(2)
            ImageGrab.grab(bbox=(x,y,x+w,y+h)).save(OUT/'scan.png')
            status=json.loads(subprocess.check_output([str(ROOT/'target/debug/devdash'),'--socket',str(socket),'status'],text=True))
            assert status['config']['groups']['robot']
            assert status['run']['status']=='', 'smoke must not operate services'
            assert gui.poll() is None
            (OUT/'result.json').write_text(json.dumps({'window':window,'size':[w,h],'core_connected':True,'service_operations':0,'screenshots':['main.png','scan-user.png','scan.png']},indent=2)+'\n')
            print((OUT/'result.json').read_text())
        finally:
            if gui is not None:
                gui.terminate()
                try: gui.wait(timeout=10)
                except subprocess.TimeoutExpired: gui.kill();gui.wait()
            daemon.send_signal(signal.SIGTERM)
            try: daemon.wait(timeout=10)
            except subprocess.TimeoutExpired: daemon.kill();daemon.wait()
            daemon_log.close()
            gui_log.close()
            nested.terminate()
            try: nested.wait(timeout=5)
            except subprocess.TimeoutExpired: nested.kill();nested.wait()
            nested_log.close()


if __name__ == '__main__':
    main()
