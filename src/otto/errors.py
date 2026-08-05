"""The root of otto's exception hierarchy.

Every exception otto DEFINES subclasses :class:`OttoError` in addition to the
stdlib type it already carried, so an existing ``except ValueError`` /
``except RuntimeError`` handler keeps working unchanged while one ``except
OttoError`` clause catches all of them at once. Place ``except OttoError``
before any broad ``except ValueError`` / ``except RuntimeError`` clause in
the same ``try`` — the first lexical match wins.

DEFINES, not raises, and the difference is not small: otto also raises plain
stdlib exceptions at 330 sites — an argument otto validates and rejects is
usually a bare ``ValueError``, not a named class. ``except OttoError``
therefore means "one of otto's two dozen NAMED failures", not "anything otto
raised".

There is no one clause that catches everything, and it is worth being exact
rather than offering a comforting near-miss:

* ``except Exception`` catches all but five raises. The five are
  ``SystemExit``, and three of them are in public library API —
  :func:`otto.lifecycle.run_command`, :func:`otto.suite.run.run_suite` and
  ``run_selection``. A caller who wraps those and expects a broad guard to
  hold gets a process exit instead.
* ``except (ValueError, RuntimeError)`` covers 284 of the 330 raise sites,
  but only 15 of the 24 named classes. Seven are rooted at plain
  ``Exception`` (the bootstrap, lab-context, lab-repository and reservation
  errors), and two sit under ``OSError`` (``AppShellTimeoutError``,
  ``LoginProxyError``).

So: catch by NAME what you intend to handle, use ``except OttoError`` when
"was this otto's own failure?" is the question, and treat ``except
Exception`` as the broad guard while knowing what passes through it. Which
stdlib root each named class carries — and which seven deliberately have
none — is declared, and gated, in ``tests/unit/test_error_base.py``.

The one deliberate exception is
:class:`~otto.lifecycle.SyncPhaseInterrupt`: it is a ``KeyboardInterrupt``,
and any ``Exception``-rooted base would make it catchable by ``except
Exception`` — which is exactly what its signal contract forbids.
"""


class OttoError(Exception):
    """Base class for every exception otto defines (not every one it raises)."""
