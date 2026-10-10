"""Replacing the built-in json lab source reaches the real product path.

``build_lab_sources`` is what every lab load goes through; a backend
registered over ``json`` with ``overwrite=True`` must be what it builds for a
``backend = "json"`` source. The root isolation fixture restores the table.
"""

from otto.labs.composite import CompositeLabRepository
from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository
from otto.labs.sources import build_lab_sources
from tests._fixtures.sutrepo import make_sut_repo

REPLACES = [("otto.labs.registry:LAB_REPOSITORIES", "json")]
"""Every ``(table, built-in name)`` pair this module replaces."""


class _Spy:
    def load_lab(self, name, preferences=None, inventory=None):
        raise NotImplementedError

    def list_labs(self):
        return ["spied"]


def test_replaces_names_every_built_in_lab_source():
    builtins = {
        ("otto.labs.registry:LAB_REPOSITORIES", name)
        for name in LAB_REPOSITORIES.names()
        if LAB_REPOSITORIES.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


def test_a_replaced_json_source_is_what_build_lab_sources_builds(tmp_path):
    from otto.config.repo import Repo

    make_sut_repo(
        tmp_path, name="r", extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n'
    )
    repo = Repo(sut_dir=tmp_path)
    spy = _Spy()
    entry = LAB_REPOSITORIES.peek("json")
    seen = []

    def factory(c):
        seen.append(c)
        return spy

    register_lab_repository("json", config=entry.config, factory=factory, overwrite=True)
    composite = build_lab_sources([repo])
    assert isinstance(composite, CompositeLabRepository)
    assert [source.repository for source in composite.sources] == [spy]
    (configured,) = seen
    assert configured.config.paths == (tmp_path / "lab_data",)
    assert configured.env.repo_dir == tmp_path
    assert configured.env.label == "r/json#1"
