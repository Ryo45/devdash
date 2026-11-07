"""Independent ROS bridge; metadata and protocol checks never import ROS."""
from importlib.metadata import PackageNotFoundError, version

IPC_PROTOCOL = 1
try:
    __version__ = version('devdash-roslaunch')
except PackageNotFoundError:
    __version__ = '0+uninstalled'


def identity():
    return {'version': __version__, 'protocol': IPC_PROTOCOL}
