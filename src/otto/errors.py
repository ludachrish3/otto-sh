"""The root of otto's exception hierarchy.

Every exception otto DEFINES subclasses :class:`OttoError` in addition to the
stdlib type it already carried, so an existing ``except ValueError`` /
``except RuntimeError`` handler keeps working unchanged while one ``except
OttoError`` clause catches all of them at once. Place ``except OttoError``
before any broad ``except ValueError`` / ``except RuntimeError`` clause in
the same ``try`` — the first lexical match wins.

DEFINES, not raises, and the difference is not small: otto also raises plain
stdlib exceptions at 301 sites — an argument otto validates and rejects is
usually a bare ``ValueError``, not a named class. ``except OttoError``
therefore means "one of otto's 46 NAMED failures", not "anything otto
raised".

There is no one clause that catches everything, and it is worth being exact
rather than offering a comforting near-miss:

* ``except Exception`` catches all but six raises. Five are ``SystemExit``,
  and three of THOSE are in public library API —
  :func:`otto.lifecycle.run_command`, :func:`otto.suite.run.run_suite` and
  ``run_selection``. A caller who wraps those and expects a broad guard to
  hold gets a process exit instead. The sixth is
  :class:`~otto.lifecycle.SyncPhaseInterrupt`, a ``KeyboardInterrupt`` on
  purpose (see below).
* ``except (ValueError, RuntimeError)`` covers 254 of the 301 raise sites,
  and 33 of the 46 named classes. Of the other 13, nine are rooted at plain
  ``Exception`` (the bootstrap, project-activation, lab-context,
  lab-repository and reservation errors) and four sit under ``OSError``
  (``AppShellTimeoutError``, ``LoginProxyError``, ``RetryAttemptTimeoutError``,
  ``WaitTimeoutError``) — 33 + 9 + 4 = 46, so the split accounts for every
  named class.

Those counts are measured, not maintained by arithmetic: a *raise site* is a
``raise`` of a name that is a BUILTIN exception type (so ``typer.Exit`` and
otto's own classes are excluded from the 301), and it is *covered* when that
builtin is rooted at ``ValueError`` or ``RuntimeError`` — which is why the 42
``NotImplementedError`` raises count as covered. Re-measure by walking the
AST of ``src/otto``; do not adjust these by hand. The 2026-08-07 error-taxonomy
wave is why the site count FELL: 37 bare ``RuntimeError`` raises in link,
tunnel, docker and transfer became named classes, which moves them out of the
301 and into the named-class total. That total is stated once, above, and
gated; it is not repeated here, because the second copy is the one that goes
stale.

So: catch by NAME what you intend to handle, use ``except OttoError`` when
"was this otto's own failure?" is the question, and treat ``except
Exception`` as the broad guard while knowing what passes through it. Which
stdlib root each named class carries — and which nine deliberately have
none — is declared, and gated, in ``tests/unit/test_error_base.py``.

The one deliberate exception is
:class:`~otto.lifecycle.SyncPhaseInterrupt`: it is a ``KeyboardInterrupt``,
and any ``Exception``-rooted base would make it catchable by ``except
Exception`` — which is exactly what its signal contract forbids.

That last point cuts both ways, and :data:`UNCONTAINABLE` below is the other
edge. A *containment seam* — code that runs user modules and turns one bad
file into a framed error rather than a crash — cannot filter on ``except
Exception``, because the most common way a user module declines to load does
not raise one. ``pytest.importorskip`` and ``pytest.skip(...,
allow_module_level=True)`` raise ``Skipped``; ``pytest.fail()`` raises
``Failed``; ``pytest.xfail()`` raises ``XFailed``. All three sit under
``OutcomeException``, which is rooted at ``BaseException`` rather than
``Exception`` — the ``pytrace`` argument has nothing to do with it. Only
``pytest.exit()``'s ``Exit`` is an ``Exception``, which is why the old seams
caught that one and nothing else: the boundary was an accident, not a design.
A seam that catches only ``Exception`` therefore lets a single
optional-dependency test file traceback out of every otto command. Such a
seam should catch ``BaseException`` and re-raise what :func:`is_containable`
rejects.
"""

#: Exceptions a containment seam must re-raise rather than frame.
#:
#: These mean "this process or task is being torn down", not "this user file is
#: broken", so absorbing them would be worse than the crash a seam prevents: a
#: user's Ctrl-C during bootstrap would become a framed ``failed to load`` line
#: and otto would carry on regardless.
#:
#: ``KeyboardInterrupt`` covers :class:`~otto.lifecycle.SyncPhaseInterrupt` by
#: subclassing — the signal contract holds here for free, which is precisely
#: why that class is rooted where it is.
#:
#: ``GeneratorExit`` is here for a specific reason, not for symmetry:
#: :func:`otto.context.open_context` is an ``@asynccontextmanager`` — an async
#: generator — with ``bootstrap()`` in its body. Swallowing a ``GeneratorExit``
#: raised while that generator is being closed would risk ``RuntimeError:
#: generator ignored GeneratorExit``.
#:
#: ``asyncio.CancelledError`` is deliberately ABSENT, and the reason is NOT
#: "no event loop is running" — ``open_context`` and
#: ``otto.config.get_repos`` both reach ``bootstrap()`` from async callers.
#: It is that ``bootstrap()`` contains no ``await``: cancellation is delivered
#: at a suspension point, and this seam has none, so a ``CancelledError`` can
#: only get here by a user module body raising it explicitly — which is user
#: code failing, and is framed like any other. Listing it would also import
#: ``asyncio`` into the composition root, which ``otto.bootstrap`` does not do
#: today; ``test_composition_root_does_not_import_asyncio`` pins that, so if
#: the premise changes the decision gets revisited instead of rotting.
#:
#: :meta hide-value:
#:     Sphinx renders an attribute's repr as a Python literal block, and
#:     ``(<class 'KeyboardInterrupt'>, ...)`` does not lex as Python — under
#:     ``-W`` that highlighting failure is a build error. The names are in the
#:     prose above, so the repr adds nothing.
UNCONTAINABLE: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)


def is_containable(exc: BaseException) -> bool:
    """Return True when a containment seam may frame *exc* instead of re-raising.

    The inverse of membership in :data:`UNCONTAINABLE`. Use as::

        try:
            run_user_code()
        except BaseException as e:
            if not is_containable(e):
                raise
            errors.append(FramedError(..., e))
    """
    return not isinstance(exc, UNCONTAINABLE)


class OttoError(Exception):
    """Base class for every exception otto defines (not every one it raises)."""


class EnsureStateError(OttoError, RuntimeError):
    """A converge could not reach the lab state it was asked to guarantee.

    Raised by the ``ensure_installed`` / ``ensure_uninstalled`` /
    ``ensure_clean`` suite fixtures when :mod:`otto.project`'s converge layer
    answers non-ok. It is an ERROR and never a skip, by house rule: a host
    that cannot be brought to the state a test requires fails that test with
    the host named, rather than quietly removing the test from the run.

    ``RuntimeError``, like the host errors whose message it usually carries,
    so the ``except (ValueError, RuntimeError)`` clauses that already bracket
    lab work keep catching it.

    It lives here rather than in ``otto.suite`` because the state it reports
    on is the PROJECT's, not the suite's; ``otto.errors`` is the zero-import
    leaf both layers already depend on, so neither has to import the other to
    name this failure.
    """
