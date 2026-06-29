from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("civsim")
except PackageNotFoundError:  # not installed as a package
    __version__ = "0.1.0-dev"

from .models import World, Civilization, Person, Era
from .engine import Simulation
from .archive import Archive

__all__ = [
    "World",
    "Civilization",
    "Person",
    "Era",
    "Simulation",
    "Archive",
    "__version__",
]