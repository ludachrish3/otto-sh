"""A declined provenance query must not fail the invocation it was describing.

:func:`otto.cli.invoke.repo_provenance` is the SECOND of two independent
defences against the preamble traceback (the first is
:attr:`~otto.host.local_host.LocalHost.dry_run_exempt`, which is why the
``except`` arm does not fire in production). It has to be tested by INJECTING
the decline: with the exemption in place the real code path never produces one,
so a test that merely ran a dry run would be green whether this function
existed or not.
"""

import pytest

from otto.cli.invoke import PROVENANCE_NOT_READ, repo_provenance
from otto.result import CommandNotRunError

pytestmark = pytest.mark.hostless


class _DecliningRepo:
    """A repo whose HEAD query was declined — the injected hostile condition."""

    sut_dir = "/sut/declining"

    @property
    def commit(self) -> str:
        raise CommandNotRunError("git -C /sut/declining log -1 --format=%H", "localhost")


class _AnsweringRepo:
    """A repo whose HEAD query succeeded."""

    sut_dir = "/sut/answering"
    commit = "1234567890abcdef1234567890abcdef12345678"


class _BrokenRepo:
    """A repo whose HEAD query failed for a reason that is NOT a dry run."""

    sut_dir = "/sut/broken"

    @property
    def commit(self) -> str:
        raise FileNotFoundError("git: command not found")


class TestRepoProvenance:
    def test_a_declined_query_is_reported_not_raised(self) -> None:
        """The decline becomes a log string, and the SHA still passes through.

        Both halves in the same test: a function that returned the stand-in
        unconditionally would satisfy the first assertion perfectly.
        """
        assert repo_provenance(_DecliningRepo()) == PROVENANCE_NOT_READ

        # POSITIVE CONTROL — the ordinary case still yields the real SHA, so
        # the arm above is a fallback and not the only path through.
        assert repo_provenance(_AnsweringRepo()) == _AnsweringRepo.commit

    def test_a_real_failure_still_propagates(self) -> None:
        """A NAMED arm, above nothing wider.

        A missing git binary is a real failure of a real query. Swallowing it
        here would turn this function into the blanket ``except`` that the
        dry-run sweep spent a commit removing from ``otto.docker``.
        """
        with pytest.raises(FileNotFoundError):
            repo_provenance(_BrokenRepo())
