"""Headless Core and optional Tkinter GUI share one installed distribution."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("devdash-python")
except PackageNotFoundError:
    __version__ = "0+uninstalled"
