"""Real repos, real hosts, a real lab, and the one lazy seam that scopes them.

Lifted out of ``tests/unit/config/test_fleet_scoping.py`` when a second module
(the reservation gate, which now computes its requirement over
``OttoContext.admissible_ids()``) needed the same construction. Real ``Repo``
objects parsed from a real ``settings.toml`` and real hosts built by the
factory — the scoping chain reads what settings parsing and lab loading
produced, so a stub of either would pin the stub's shape instead of the chain's.
"""

import textwrap

from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.context import OttoContext, set_context
from tests._fixtures.sutrepo import make_sut_repo


def _toml_list(patterns):
    # TOML literal strings (single quotes) keep backslashes verbatim.
    return "[" + ", ".join(f"'{p}'" for p in patterns) + "]"


def _repo(tmp_path, name, *, labs=None, hosts=None):
    """A real ``Repo`` whose ``[project]`` block declares *labs* / *hosts*.

    Both ``None`` writes no ``[project]`` table at all — the undeclared repo the
    whole-lab fallback is built on, which is a different thing from a repo that
    declared an empty list.
    """
    body = ""
    if labs is not None or hosts is not None:
        lines = ["[project]"]
        if labs is not None:
            lines.append(f"lab_patterns = {_toml_list(labs)}")
        if hosts is not None:
            lines.append(f"host_patterns = {_toml_list(hosts)}")
        body = "\n".join(lines)
    return Repo(sut_dir=make_sut_repo(tmp_path / name, name=name, extra=textwrap.dedent(body)))


def _host(element, lab_name, octet):
    from otto.host.factory import create_host_from_dict

    return create_host_from_dict(
        {
            "element": element,
            "os_type": "unix",
            "ip": f"10.0.0.{octet}",
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name=lab_name,
    )


def _lab(*pairs, component_names=None):
    """A ``Lab`` holding ``(element, source_lab)`` hosts, stamped by the factory."""
    names = list(component_names or sorted({lab_name for _, lab_name in pairs}))
    lab = Lab(name="+".join(names), component_names=names)
    for octet, (element, lab_name) in enumerate(pairs, start=1):
        lab.add_host(_host(element, lab_name, octet))
    return lab


def add_builtin_local(lab, *, resources=frozenset()):
    """Inject the built-in ``LocalHost``, the way ``load_lab`` does.

    *resources* is the hostile condition, not a realistic one: the built-in host
    carries none in production, so a guard that merely observed "``local``
    required nothing" would pass against a gate that never excluded it. Handing
    it a resource is what makes the exclusion falsifiable.
    """
    from otto.host.builtin_hosts import make_builtin_local_host

    local_host = make_builtin_local_host()
    local_host.source_lab = lab.component_names[0]
    local_host.resources = frozenset(resources)
    lab.add_host(local_host)
    return lab


def install_scoped_context(monkeypatch, lab, repos):
    """Build and install an ``OttoContext`` whose scopes resolve over *repos*.

    ``OttoContext.scopes`` reads ``otto.config.get_ordered_repos`` lazily, so
    patching that one seam is what lets a unit test declare a fleet of interest
    without standing up a bootstrap. An empty *repos* list is the whole-lab
    fallback — no declaration, no narrowing.

    Installation is not undone here: the root conftest's autouse
    ``_reset_otto_context`` snapshot-restores the ContextVar after every test,
    and a ``reset_context(token)`` of our own cannot work anyway — async callers
    run their body in a COPY of the context, so the token is from a different
    Context object and ``ContextVar.reset`` raises.
    """
    monkeypatch.setattr("otto.config.get_ordered_repos", lambda: list(repos))
    ctx = OttoContext(lab=lab)
    set_context(ctx)
    return ctx
