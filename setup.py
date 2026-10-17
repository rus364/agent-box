"""Supply only metadata that depends on the release version to the PEP 517 build."""
from pathlib import Path
import sys

from setuptools import setup

ROOT = Path(__file__).resolve().parent
# PEP 517 does not put the source directory on sys.path when executing setup.py.
sys.path.insert(0, str(ROOT))
from tools.package_metadata import distribution_metadata  # noqa: E402

setup(**distribution_metadata(ROOT))
