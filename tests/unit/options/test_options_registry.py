"""The per-verb options registry: register_options, the verbs it accepts, and what it refuses."""

import inspect
import sys
from typing import Annotated

import pytest
import typer

from otto import options
from otto.params import (
    OPTIONS,
    OptionsCollisionError,
    OptionsOrigin,
    OptionsRegistrationError,
    flatten_option_instances,
    merge_option_params,
    options_key,
    register_options,
    verb_option_classes,
    verbs_for,
)
from otto.registry import DuplicateRegistration


@options
class Base:
    lab_env: Annotated[str, typer.Option(help="Lab.")] = "staging"


@options
class ForTestOnly(Base):
    firmware: str = "latest"


@options
class RunOnly(Base):
    debug: bool = False


@options
class Rival:
    lab_env: str = "prod"


def test_registering_names_the_verbs_and_keeps_order():
    register_options(Base, verbs=["run", "test"])
    register_options(ForTestOnly, verbs=["test"])
    assert [o.cls for o in verb_option_classes("test")] == [Base, ForTestOnly]
    assert [o.cls for o in verb_option_classes("run")] == [Base]
    assert verbs_for(ForTestOnly) == ["test"]
    assert verbs_for(Rival) is None


@pytest.mark.parametrize(
    ("verbs", "match"),
    [
        (["deploy"], "unknown verb 'deploy'; verbs that accept options: run, test"),
        ([], "names no verbs"),
        (["run", "run"], "names 'run' twice"),
        ("test", r'verbs must be a list of verb names, such as \["test"\]; got \'test\''),
    ],
)
def test_bad_verbs_raise_at_registration(verbs, match):
    with pytest.raises(OptionsRegistrationError, match=match):
        register_options(Base, verbs=verbs)
    assert options_key(Base) not in OPTIONS


def test_registering_one_class_twice_raises_even_through_its_string_form():
    register_options(Base, verbs=["run"])
    with pytest.raises(DuplicateRegistration, match="already registered"):
        register_options(f"{__name__}:Base", verbs=["test"])


def test_the_string_form_is_not_imported_until_the_verb_needs_it(tmp_path, monkeypatch):
    pkg = tmp_path / "lazyopts_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from otto import options\n@options\nclass Lazy:\n    x: int = 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "lazyopts_pkg", raising=False)
    register_options("lazyopts_pkg:Lazy", verbs=["test"])
    assert "lazyopts_pkg" not in sys.modules
    assert verb_option_classes("run") == []
    assert "lazyopts_pkg" not in sys.modules
    (origin,) = verb_option_classes("test")
    assert origin.cls.__name__ == "Lazy"


def test_a_string_registration_resolved_while_test_files_load_may_register_on_import(
    tmp_path, monkeypatch
):
    """The lazy form's module belongs to the init module that named it, wherever it resolves.

    Resolution may first happen inside a pytest session, i.e. while test files
    load; the module it imports then registers a companion class of its own.
    That registration was named by an init module, not made by a test file, so
    it is not refused. The phase marker is back in force once the import is
    done: the test file's own registration right after is still refused.
    """
    from otto.registry import RegistrationRefused, is_loading_test_files, loading_test_files

    pkg = tmp_path / "lateresolve_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from otto import options\n"
        "@options\nclass Lazy:\n    x: int = 1\n"
        "@options(verbs=['run'])\nclass Companion:\n    y: int = 2\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "lateresolve_pkg", raising=False)
    register_options("lateresolve_pkg:Lazy", verbs=["test"])
    with loading_test_files():
        (origin,) = verb_option_classes("test")
        assert is_loading_test_files()
        with pytest.raises(RegistrationRefused):
            register_options(Base, verbs=["test"])
    assert origin.cls.__name__ == "Lazy"
    assert [o.cls.__name__ for o in verb_option_classes("run")] == ["Companion"]


def test_a_string_naming_a_re_export_is_refused_on_resolution(tmp_path, monkeypatch):
    pkg = tmp_path / "reexport_pkg"
    pkg.mkdir()
    (pkg / "defining.py").write_text(
        "from otto import options\n@options\nclass Real:\n    x: int = 1\n"
    )
    (pkg / "__init__.py").write_text("from .defining import Real\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "reexport_pkg", raising=False)
    monkeypatch.delitem(sys.modules, "reexport_pkg.defining", raising=False)
    register_options("reexport_pkg:Real", verbs=["test"])
    with pytest.raises(OptionsRegistrationError, match="re-export") as err:
        verb_option_classes("test")
    text = str(err.value)
    assert "reexport_pkg:Real" in text
    assert "reexport_pkg.defining:Real" in text


def test_a_malformed_string_raises_at_registration():
    with pytest.raises(ValueError, match=r"package\.module:attribute"):
        register_options("no_colon_here", verbs=["test"])


def test_a_field_from_one_shared_base_is_one_parameter():
    params = merge_option_params(
        [OptionsOrigin(ForTestOnly, "repo_a"), OptionsOrigin(RunOnly, "repo_b")], what="otto test"
    )
    assert [p.name for p in params] == ["lab_env", "firmware", "debug"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)


def test_the_same_name_from_unrelated_classes_names_both_classes_and_repos():
    with pytest.raises(OptionsCollisionError) as err:
        merge_option_params(
            [OptionsOrigin(Base, "repo_a"), OptionsOrigin(Rival, "repo_b")], what="otto test"
        )
    text = str(err.value)
    assert "otto test" in text
    assert "lab_env" in text
    assert "repo 'repo_a'" in text
    assert "repo 'repo_b'" in text
    assert "Base" in text
    assert "Rival" in text


def test_a_field_named_like_an_otto_flag_collides():
    @options
    class Clash:
        iterations: int = 3

    with pytest.raises(OptionsCollisionError, match="otto test's --iterations"):
        merge_option_params(
            [OptionsOrigin(Clash, "repo_a")],
            what="otto test",
            reserved={"iterations": "otto test's --iterations"},
        )


def test_flattening_instances_merges_fields_and_refuses_unregistered_or_conflicting():
    register_options(ForTestOnly, verbs=["test"])
    register_options(RunOnly, verbs=["run", "test"])
    flat = flatten_option_instances([ForTestOnly(firmware="2.1")], verb="test")
    assert flat == {"lab_env": "staging", "firmware": "2.1"}
    with pytest.raises(OptionsRegistrationError, match="Rival is not registered") as err:
        flatten_option_instances([Rival()], verb="test")
    assert "@otto.options(verbs=['test', ...]) in an init module" in str(err.value)
    with pytest.raises(ValueError, match="lab_env"):
        flatten_option_instances([ForTestOnly(lab_env="a"), RunOnly(lab_env="b")], verb="test")


def test_registration_while_test_files_load_is_refused():
    from otto.registry import RegistrationRefused, loading_test_files

    with loading_test_files(), pytest.raises(RegistrationRefused):
        register_options(Base, verbs=["test"])


def test_the_decorator_registers_only_when_given_verbs():
    @options(verbs=["test"])
    class Sugared:
        firmware: str = "latest"

    @options
    class Plain:
        x: int = 0

    assert verbs_for(Sugared) == ["test"]
    assert OPTIONS.origin(options_key(Sugared)) == __name__
    assert verbs_for(Plain) is None
    assert Sugared(firmware="2.1").firmware == "2.1"


def test_the_decorator_validates_like_pydantic_and_passes_its_arguments_through():
    import pydantic

    @options(verbs=["run"], config=pydantic.ConfigDict(str_strip_whitespace=True))
    class Stripped:
        name: str = ""

    assert Stripped(name="  x  ").name == "x"
    with pytest.raises(pydantic.ValidationError, match="name"):
        Stripped(name=3)  # type: ignore[arg-type]


def test_a_subclass_inherits_its_bases_config():
    import pydantic

    @options(config=pydantic.ConfigDict(str_strip_whitespace=True))
    class ConfiguredBase:
        name: str = ""

    @options
    class Child(ConfiguredBase):
        extra: str = ""

    assert Child(name="  x  ", extra="  y  ").name == "x"


def test_a_class_body_config_is_honored_with_no_warning():
    import pydantic

    @options
    class WithOwnConfig:
        __pydantic_config__ = pydantic.ConfigDict(str_strip_whitespace=True)
        name: str = ""

    # No pytest.warns / catch_warnings needed: this repo's pytest config
    # turns every warning into an error (filterwarnings = ["error", ...]),
    # so decorating above already proved pydantic's "config set via both
    # the decorator and __pydantic_config__" warning did not fire.
    assert WithOwnConfig(name="  x  ").name == "x"


def test_a_reused_decorator_keeps_its_config_for_every_class():
    import pydantic

    deco = options(config=pydantic.ConfigDict(str_strip_whitespace=True))

    @deco
    class First:
        name: str = ""

    @deco
    class Second:
        name: str = ""

    assert First(name="  x  ").name == "x"
    assert Second(name="  y  ").name == "y"


def test_the_decorator_refuses_bad_verbs_and_a_second_registration():
    with pytest.raises(OptionsRegistrationError, match="names no verbs"):

        @options(verbs=[])
        class Empty:
            x: int = 0

    @options(verbs=["run"])
    class Once:
        x: int = 0

    with pytest.raises(DuplicateRegistration, match="already registered"):
        register_options(Once, verbs=["test"])


def test_the_decorator_is_refused_while_test_files_load():
    from otto.registry import RegistrationRefused, loading_test_files

    with loading_test_files(), pytest.raises(RegistrationRefused):

        @options(verbs=["test"])
        class Late:
            x: int = 0


def test_an_unknown_options_key_names_the_decorator_as_the_remedy():
    """A registry lookup miss points at ``@otto.options(verbs=[...])`` in an init module."""
    from otto.params import OPTIONS

    with pytest.raises(ValueError, match=r"@otto\.options\(verbs=\[\.\.\.\]\) in an init module"):
        OPTIONS.get("nowhere:Missing")
