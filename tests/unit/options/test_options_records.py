"""Options registrations are checked records: key, verbs, duplicates and the repo."""

import pytest

from otto.params import (
    OPTIONS,
    OptionsEntry,
    OptionsRegistrationError,
    options,
    register_options,
    verb_option_classes,
)
from otto.registry import DuplicateRegistration
from tests._fixtures.registrant import from_module


@options
class Opts:
    flag: bool = False


def test_a_second_registration_raises_the_uniform_duplicate():
    register_options(Opts, verbs=["run"])
    with pytest.raises(DuplicateRegistration, match="overwrite=True"):
        register_options(Opts, verbs=["test"])


def test_overwrite_replaces_the_verb_list():
    register_options(Opts, verbs=["run"])
    register_options(Opts, verbs=["run", "test"], overwrite=True)
    assert OPTIONS.get(f"{__name__}:Opts").verbs == ("run", "test")


def test_a_raw_entry_under_the_wrong_key_is_refused():
    with pytest.raises(OptionsRegistrationError, match="key"):
        OPTIONS.register("wrong:Key", OptionsEntry(target=Opts, verbs=("run",)))


def test_the_repo_comes_from_the_engine():
    from otto.registry import registering_repo

    with registering_repo("acme"):
        register_options(Opts, verbs=["run"])
    assert OPTIONS.repo(f"{__name__}:Opts") == "acme"
    assert verb_option_classes("run")[-1].repo == "acme"


def test_a_raw_entry_with_an_unknown_verb_is_refused():
    with pytest.raises(OptionsRegistrationError, match="unknown verb 'deploy'"):
        OPTIONS.register(f"{__name__}:Opts", OptionsEntry(target=Opts, verbs=("deploy",)))
    assert f"{__name__}:Opts" not in OPTIONS


def test_the_direct_decorator_call_credits_the_calling_module():
    class Direct:
        flag: bool = False

    built = from_module("case_direct.init", options, Direct, verbs=["run"])
    assert OPTIONS.origin(f"{__name__}:{built.__qualname__}") == "case_direct.init"


def _declare(verbs: list, **kwargs: object) -> type:
    """Decorate a new class whose key is the same on every call."""

    class Same:
        flag: bool = False

    return options(verbs=verbs, **kwargs)(Same)


def test_the_decorator_passes_overwrite_through():
    built = _declare(["run"])
    key = f"{__name__}:{built.__qualname__}"
    with pytest.raises(DuplicateRegistration):
        _declare(["test"])
    assert OPTIONS.peek(key).verbs == ("run",)
    _declare(["test"], overwrite=True)
    assert OPTIONS.peek(key).verbs == ("test",)
