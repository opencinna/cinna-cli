"""cinna-cli — Local development CLI for Cinna Core agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cinna-cli")
except PackageNotFoundError:  # not installed (e.g. running from a source tree without metadata)
    __version__ = "0.0.0+unknown"
