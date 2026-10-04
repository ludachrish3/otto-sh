"""Self-test for ``tests/_fixtures/fake_repo.py``: the double is complete by construction."""

from pathlib import Path

import pytest

from otto.config.repo import Repo
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.sutrepo import make_sut_repo


def test_a_fake_repo_has_every_attribute_a_parsed_repo_has(tmp_path: Path) -> None:
    real = Repo(sut_dir=make_sut_repo(tmp_path, name="real"))
    fake = fake_repo()

    assert type(fake) is Repo
    assert sorted(vars(fake)) == sorted(vars(real))
    assert sorted(dir(fake)) == sorted(dir(real))


def test_unparsed_fields_carry_repos_own_defaults() -> None:
    fake = fake_repo("acme")

    assert fake.name == "acme"
    assert fake.project_scope is None
    assert fake.inventory_settings == {}
    assert fake.creds_settings == {}
    assert fake.reservation_settings == {}
    assert list(fake.docker_settings.use_cases) == []


def test_mutable_defaults_are_not_shared_between_doubles() -> None:
    a, b = fake_repo("a"), fake_repo("b")
    a.dependencies.append("x")  # type: ignore[arg-type]

    assert b.dependencies == []


def test_overrides_apply() -> None:
    scope = object()
    fake = fake_repo(
        "acme",
        sut_dir=Path("/srv/acme"),
        project_scope=scope,
        settings={"inventory": {"backend": "json"}},
        get_lab_panel=lambda: "panel",
    )

    assert fake.sut_dir == Path("/srv/acme")
    assert fake.project_scope is scope
    assert fake.inventory_settings == {"backend": "json"}
    assert fake.get_lab_panel() == "panel"


@pytest.mark.parametrize("key", ["inventory_settings", "creds_settings", "reservation_settings"])
def test_a_settings_view_is_refused_in_favour_of_settings(key: str) -> None:
    with pytest.raises(TypeError, match=r"pass settings="):
        fake_repo(**{key: {}})


def test_an_attribute_repo_does_not_have_is_refused() -> None:
    with pytest.raises(TypeError, match="no attribute 'labs'"):
        fake_repo(labs=[])
