"""Replacing a built-in lab source reaches the real product path.

``build_lab_sources`` is what every lab load goes through; a backend
registered over ``json`` with ``overwrite=True``, or over the yielding
``jsonnet`` without it, must be what it builds for a source naming that
backend. The root isolation fixture restores the table.
"""

from otto.labs.composite import CompositeLabRepository
from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository
from otto.labs.sources import build_lab_sources
from tests._fixtures.project_scaffold import make_project

REPLACES = [
    ("otto.labs.registry:LAB_REPOSITORIES", "json"),
    ("otto.labs.registry:LAB_REPOSITORIES", "jsonnet"),
]
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
        if LAB_REPOSITORIES.origin(name=name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


def test_a_replaced_json_source_is_what_build_lab_sources_builds(tmp_path):
    from otto.config.project import Project

    make_project(
        tmp_path, name="r", extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n'
    )
    project = Project(root=tmp_path)
    spy = _Spy()
    entry = LAB_REPOSITORIES.peek(name="json")
    seen = []

    def factory(c):
        seen.append(c)
        return spy

    register_lab_repository("json", config=entry.config, factory=factory, overwrite=True)
    composite = build_lab_sources(projects=[project])
    assert isinstance(composite, CompositeLabRepository)
    assert [source.repository for source in composite.sources] == [spy]
    (configured,) = seen
    assert configured.config.paths == (tmp_path / "lab_data",)
    assert configured.env.project_root == tmp_path
    assert configured.env.label == "r/json#1"


def test_a_replaced_jsonnet_source_is_what_build_lab_sources_builds(tmp_path):
    from otto.config.project import Project

    make_project(
        tmp_path,
        name="r",
        extra='[[lab.sources]]\nbackend = "jsonnet"\npaths = ["lab_data/layouts"]\n',
    )
    project = Project(root=tmp_path)
    spy = _Spy()
    entry = LAB_REPOSITORIES.peek(name="jsonnet")
    seen = []

    def factory(c):
        seen.append(c)
        return spy

    # jsonnet yields its name: no overwrite=True is needed to replace it.
    register_lab_repository("jsonnet", config=entry.config, factory=factory)
    composite = build_lab_sources(projects=[project])
    assert [source.repository for source in composite.sources] == [spy]
    (configured,) = seen
    assert configured.config.paths == (tmp_path / "lab_data" / "layouts",)
    assert configured.env.project_name == "r"
    assert configured.env.label == "r/jsonnet#1"
