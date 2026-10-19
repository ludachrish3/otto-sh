"""Self-test for ``tests/_fixtures/fake_project.py``: the double is complete by construction."""

from pathlib import Path

import pytest

from otto.config.project import Project
from tests._fixtures.fake_project import fake_project
from tests._fixtures.project_scaffold import make_project


def test_a_fake_project_has_every_attribute_a_parsed_project_has(tmp_path: Path) -> None:
    real = Project(root=make_project(tmp_path, name="real"))
    fake = fake_project()

    assert type(fake) is Project
    assert sorted(vars(fake)) == sorted(vars(real))
    assert sorted(dir(fake)) == sorted(dir(real))


def test_unparsed_fields_carry_projects_own_defaults() -> None:
    fake = fake_project("acme")

    assert fake.name == "acme"
    assert fake.project_scope is None
    assert fake.inventory_settings == {}
    assert fake.creds_settings == {}
    assert fake.reservation_settings == {}
    assert list(fake.docker_settings.use_cases) == []


def test_mutable_defaults_are_not_shared_between_doubles() -> None:
    a, b = fake_project("a"), fake_project("b")
    a.dependencies.append("x")  # type: ignore[arg-type]

    assert b.dependencies == []


def test_overrides_apply() -> None:
    scope = object()
    fake = fake_project(
        "acme",
        root=Path("/srv/acme"),
        project_scope=scope,
        settings={"inventory": {"backend": "json"}},
        get_lab_panel=lambda: "panel",
    )

    assert fake.root == Path("/srv/acme")
    assert fake.project_scope is scope
    assert fake.inventory_settings == {"backend": "json"}
    assert fake.get_lab_panel() == "panel"


@pytest.mark.parametrize("key", ["inventory_settings", "creds_settings", "reservation_settings"])
def test_a_settings_view_is_refused_in_favour_of_settings(key: str) -> None:
    with pytest.raises(TypeError, match=r"pass settings="):
        fake_project(**{key: {}})


def test_an_attribute_project_does_not_have_is_refused() -> None:
    with pytest.raises(TypeError, match="no attribute 'labs'"):
        fake_project(labs=[])
