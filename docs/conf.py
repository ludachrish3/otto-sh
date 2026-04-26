import importlib.metadata
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

project = "otto"
author = "otto contributors"
release = importlib.metadata.version("otto-sh")
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.napoleon",
    "sphinx_autodoc_typehints",
    "myst_parser",
    "sphinx_immaterial",
]

source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}

html_theme = "sphinx_immaterial"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_theme_options = {
    "palette": [
        {
            "media": "(prefers-color-scheme)",
            "scheme": "default",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-auto",
                "name": "Switch to dark mode",
            }
        },
        {
            "media": "(prefers-color-scheme: dark)",
            "scheme": "slate",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-4",
                "name": "Switch to light mode",
            }
        },
        {
            "media": "(prefers-color-scheme: light)",
            "scheme": "default",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-7",
                "name": "Switch to system preference",
            }
        },
    ]
}

exclude_patterns = ["RESTRUCTURE_PLAN.md"]

# -- autodoc ------------------------------------------------------------------

autodoc_member_order = "bysource"
autodoc_default_options = {
    'members': True,
    'undoc-members': True,
    'show-inheritance': True,
}
autodoc_typehints = 'signature'

# -- napoleon -----------------------------------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_param = False
napoleon_use_rtype = False
napoleon_attr_annotations = True

# -- doctest ------------------------------------------------------------------

doctest_global_setup = """
import asyncio
from otto.utils import Status, CommandStatus, splitOnCommas
from otto.host.localHost import LocalHost
from otto.monitor.parsers import human_readable

# Use a single persistent loop across all run() calls in a doctest block.
# asyncio.run() creates and closes a fresh loop each call, which breaks any
# LocalHost whose underlying ShellSession was lazily bound to the first loop
# (the second call raises "Future attached to a different loop").
_loop = asyncio.new_event_loop()

def run(coro):
    return _loop.run_until_complete(coro)
"""

# -- myst-parser --------------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "fieldlist",
    "deflist",
]

myst_heading_anchors = 3
