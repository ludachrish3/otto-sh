"""The width and colour every docs capture renders at: one table, read by both capture scripts.

``scripts/refresh_docs_captures.py`` (``RunContext.pinned_env``) and
``scripts/capture_docs_termynal.py`` (``_otto_env``) both spread
``DISPLAY_ENV`` into the environment of every command they capture, so a
committed capture and a termynal block render alike. A leaf module, so the
termynal stamp can cover exactly this decision.
"""

DISPLAY_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "100", "LINES": "50"}
"""Colour off, a dumb terminal, and 100 columns by 50 lines.

100 columns is the widest text the docs content column shows without
scrolling in a 1440px window.
"""
