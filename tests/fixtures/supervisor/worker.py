from pathlib import Path
import time
Path('/tmp/devdash-healthy').write_text('container-defined health condition\n')
print('devdash isolated supervisor worker ready', flush=True)
time.sleep(3600)
