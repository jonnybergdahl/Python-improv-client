import os
import sys

sys.path.insert(0, os.path.abspath("../src"))

from improv_client import __version__  # noqa: E402

project = "improv-client"
copyright = "2023-2026, Jonny Bergdahl"
author = "Jonny Bergdahl"
release = __version__
version = __version__

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

# Build the docs without the optional transport dependencies installed.
autodoc_mock_imports = ["bleak", "bleak_retry_connector", "serial_asyncio"]
autodoc_member_order = "bysource"
autodoc_typehints = "description"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "bleak": ("https://bleak.readthedocs.io/en/latest/", None),
    "pyserial": ("https://pyserial.readthedocs.io/en/latest/", None),
}

exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"
