"""Every otto field named like a secret is a ``SecretStr`` or hidden from repr (#487).

Traceback locals stay on in the CLI and the logs, so any object a frame holds
is rendered with its repr. A field named like a password, passphrase, secret
or token must therefore never repr its value: it is typed ``SecretStr`` (or a
type containing one), or declared ``repr=False``. The rule is the one
:func:`otto.params.sensitive_field_names` already applies to CLI options; this
guard asks it of every dataclass and pydantic model otto defines.
"""

import dataclasses
import importlib
import inspect
import re

import pydantic

import otto
from otto.params import sensitive_field_names
from tests._fixtures.otto_modules import otto_module_names

SECRET_NAME = re.compile(r"pass(word|wd|phrase)|secret|token", re.IGNORECASE)

NOT_SECRET: dict[str, str] = {
    "otto.host.daemon.DaemonProcess.token": "the sentinel tag a daemon shows in ps output",
    "otto.tunnel.check._Run.token": "the random tag a tunnel check echoes on its hosts",
    "otto.host.option_specs.ConsoleOptionsSpec.password_prompt": (
        "the prompt pattern a console login waits for"
    ),
    "otto.host.options.ConsoleOptions.password_prompt": (
        "the prompt pattern a console login waits for"
    ),
    "otto.host.os_profile.ProfileFields.password_prompt": (
        "the prompt pattern an OS profile's login waits for"
    ),
    "otto.inventory.config.NetBoxInventoryConfig.token_env": (
        "the name of the environment variable that holds the token, not the token"
    ),
}
"""Fields named like a secret that do not hold one, each with what it holds instead."""

UNIMPORTABLE: dict[str, str] = {}
"""Modules the walk may skip, each with the optional dependency it lacks.

Empty today: every otto module imports in the dev venv. A module added here is
a module the guard no longer checks, so each entry names why.
"""


def _field_names(cls: type) -> list[str]:
    if isinstance(cls, type) and issubclass(cls, pydantic.BaseModel):
        return list(cls.model_fields)
    return [f.name for f in dataclasses.fields(cls)]


def _otto_record_classes() -> list[type]:
    seen: dict[str, type] = {}
    failures: list[str] = []
    for name in otto_module_names():
        if name in UNIMPORTABLE:
            continue
        try:
            module = importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 — every failure is reported below
            failures.append(f"{name}: {type(e).__name__}: {e}")
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if not cls.__module__.startswith(f"{otto.__name__}."):
                continue
            if dataclasses.is_dataclass(cls) or issubclass(cls, pydantic.BaseModel):
                seen[f"{cls.__module__}.{cls.__qualname__}"] = cls
    assert failures == [], "otto modules the guard could not import (list in UNIMPORTABLE)"
    return [seen[k] for k in sorted(seen)]


def test_the_walk_finds_the_known_secret_fields():
    """The guard is only as good as its walk: it must reach the classes #487 fixed."""
    classes = {f"{c.__module__}.{c.__qualname__}" for c in _otto_record_classes()}
    for known in (
        "otto.models.base.Cred",
        "otto.models.base.CredSpec",
        "otto.host.telnet.TelnetClient",
        "otto.host.console.ConsoleClient",
    ):
        assert known in classes


def test_every_secret_named_field_is_hidden():
    offenders: list[str] = []
    for cls in _otto_record_classes():
        named = [
            n
            for n in _field_names(cls)
            if SECRET_NAME.search(n)
            and f"{cls.__module__}.{cls.__qualname__}.{n}" not in NOT_SECRET
        ]
        if not named:
            continue
        hidden = sensitive_field_names(cls)
        offenders += [f"{cls.__module__}.{cls.__qualname__}.{n}" for n in named if n not in hidden]
    assert offenders == []


def test_every_not_secret_entry_names_a_field_the_walk_finds():
    """An exemption whose field was renamed or removed would silently exempt nothing."""
    fields = {
        f"{c.__module__}.{c.__qualname__}.{n}"
        for c in _otto_record_classes()
        for n in _field_names(c)
    }
    assert sorted(set(NOT_SECRET) - fields) == []


def test_the_parsed_data_secret_keys_are_exactly_the_secret_model_fields():
    """``wrap_secret_values`` wraps the keys :data:`SECRET_DATA_KEYS` names before any
    model sees the parsed data; a new ``SecretStr`` field on a model must join it."""
    from otto.models.base import SECRET_DATA_KEYS

    secret_fields = set()
    for cls in _otto_record_classes():
        if issubclass(cls, pydantic.BaseModel):
            secret_fields |= sensitive_field_names(cls)
    assert secret_fields == set(SECRET_DATA_KEYS)


def test_an_annotation_that_cannot_be_resolved_never_hides_a_secret():
    """On 3.14 an annotation naming a type-checking-only import is read as a
    ``ForwardRef`` rather than raising; one naming ``SecretStr`` still counts."""
    from typing import ForwardRef

    from otto.params import contains_secret_type

    assert contains_secret_type(ForwardRef("SecretStr | None"))
    assert contains_secret_type("list[SecretBytes]")
    assert not contains_secret_type(ForwardRef("SSHClientConnection"))
