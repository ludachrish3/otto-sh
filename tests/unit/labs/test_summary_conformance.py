"""The `SupportsHostSummaries` contract is about FIELDS, not just ids.

A summary drives four completion surfaces — `--lab` scoping (`labs`), the
positional handles (`element`/`element_id`), `otto docker --on`
(`docker_capable`) and tunnel narrowing (`ip`). A backend that fills in only
`id` used to pass every rule while silently breaking all four.
"""

import pytest

from otto.config.lab import Lab
from otto.host.unix_host import UnixHost
from otto.labs import HostSummary, LabNotFoundError
from otto.models.host import Cred
from otto.testing.conformance import assert_lab_repository_conforms


def _host(element: str, *, ip: str = "10.0.0.1", docker: bool = False) -> UnixHost:
    return UnixHost(
        ip=ip,
        element=element,
        creds=[Cred(login="u", password="p")],
        docker_capable=docker,
    )


class _Backend:
    """A conforming backend; each test degrades exactly one thing."""

    def __init__(self, summaries: list[HostSummary] | None = None) -> None:
        self._hosts = {h.id: h for h in (_host("test1", docker=True), _host("test2"))}
        self._summaries = summaries

    def list_labs(self) -> list[str]:
        return ["unix", "unix_alt"]

    def load_lab(self, name: str, preferences: dict | None = None) -> Lab:
        if name not in ("unix", "unix_alt"):
            # The contract's own probe asks for a lab that cannot exist, and
            # requires this exact type — a KeyError reads as a backend bug.
            raise LabNotFoundError(name)
        lab = Lab(name=name)
        # `unix_alt` loads but holds none of these hosts: without a second real
        # lab, "claims a lab it is not in" has nothing to claim.
        if name == "unix":
            lab.hosts.update(self._hosts)
        return lab

    def list_host_summaries(self) -> list[HostSummary]:
        if self._summaries is not None:
            return self._summaries
        return [
            HostSummary(
                id=h.id,
                labs=["unix"],
                ip=h.ip,
                element=h.element,
                element_id=h.element_id,
                docker_capable=h.docker_capable,
            )
            for h in self._hosts.values()
        ]


def test_a_faithful_backend_conforms() -> None:
    """Positive control — every rule below must be failing on its own merits."""
    assert_lab_repository_conforms(_Backend(), expected_labs=["unix", "unix_alt"])


def _summaries_of() -> list[HostSummary]:
    return _Backend().list_host_summaries()


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("ip", "192.0.2.99"),
        ("element", "not-test1"),
        ("element_id", 7),
        ("docker_capable", False),
    ],
)
def test_a_field_that_disagrees_with_the_built_host_is_a_violation(field, wrong) -> None:
    """Each field, individually — a sweep that only checked `ip` would pass
    a backend lying about `docker_capable`."""
    summaries = _summaries_of()
    summaries[0] = HostSummary(
        **{
            **{
                f: getattr(summaries[0], f)
                for f in ("id", "labs", "ip", "element", "element_id", "docker_capable")
            },
            field: wrong,
        }
    )
    with pytest.raises(AssertionError, match=field):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])


def test_omitting_a_constructed_host_is_a_violation() -> None:
    """The direction nothing used to check: the completer just quietly stops
    offering that host, with no error anywhere."""
    summaries = _summaries_of()[:1]
    with pytest.raises(AssertionError, match="omits them"):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])


def test_a_labless_summary_loses_lab_scoped_completion() -> None:
    """`labs=[]` is the single easiest field to forget, and it silently
    empties `otto host <TAB>` under `-l <lab>`."""
    summaries = _summaries_of()
    summaries[0] = HostSummary(
        id=summaries[0].id,
        labs=[],
        ip=summaries[0].ip,
        element=summaries[0].element,
        element_id=summaries[0].element_id,
        docker_capable=summaries[0].docker_capable,
    )
    with pytest.raises(AssertionError, match="--lab-scoped completion would drop it"):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])


def test_an_id_no_lab_produces_is_still_a_violation() -> None:
    """The original rule, kept: offering an id that cannot dispatch is worse
    than offering none."""
    summaries = _summaries_of()
    summaries.append(HostSummary(id="ghost_seed", labs=["unix"], ip="1.1.1.1", element="ghost"))
    with pytest.raises(AssertionError, match="cannot dispatch"):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])


def test_claiming_a_lab_that_does_not_contain_the_host_is_a_violation() -> None:
    """The other half of the `labs` rule, and the load-bearing one again.

    Completion buckets by `summary.labs`, so a host claiming a lab it is not
    in gets offered by `otto host -l <that lab> <TAB>` and then fails
    "unknown host" on dispatch — the exact failure the id rule prevents, one
    surface over. Under-claiming was checked; over-claiming was not.
    """
    summaries = _summaries_of()
    first = summaries[0]
    summaries[0] = HostSummary(
        id=first.id,
        labs=["unix", "unix_alt"],  # `unix_alt` loads, and does not hold it
        ip=first.ip,
        element=first.element,
        element_id=first.element_id,
        docker_capable=first.docker_capable,
    )
    with pytest.raises(AssertionError, match="cannot dispatch"):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])


def test_a_float_element_id_is_a_violation() -> None:
    """`7 == 7.0` in Python, so an un-normalized comparison misses exactly the
    divergence `host_identity` exists to prevent (`dut3.0` vs `dut3`)."""
    summaries = _summaries_of()
    first = summaries[0]
    summaries[0] = HostSummary(
        id=first.id,
        labs=first.labs,
        ip=first.ip,
        element=first.element,
        element_id=0.0 if first.element_id is None else float(first.element_id),
        docker_capable=first.docker_capable,
    )
    with pytest.raises(AssertionError, match="element_id"):
        assert_lab_repository_conforms(_Backend(summaries), expected_labs=["unix", "unix_alt"])
