"""A field's secret type is found even when another annotation cannot be resolved.

An annotation naming a type imported only for type checking makes
:func:`typing.get_type_hints` raise ``NameError`` on every Python, and the
dry-run's ``_options_line`` and ``otto run``'s option build both ask
:func:`~otto.params.sensitive_field_names` of a user's options class. These
tests run on every interpreter: the fallback's two branches (3.14's
``annotationlib``, below it ``inspect``) are the version-specific part.
"""

import dataclasses
import os
from typing import TYPE_CHECKING, Annotated, ForwardRef

import pytest
from pydantic import Secret, SecretStr

from otto.cli.invoke import _options_line
from otto.params import contains_secret_type, sensitive_field_names

if TYPE_CHECKING:
    from fractions import Fraction


@dataclasses.dataclass
class _Base:
    token: Annotated[SecretStr | None, "inherited, and Annotated"] = None


@dataclasses.dataclass
class _Opts(_Base):
    x: "Fraction | None" = None
    pw: SecretStr | None = None
    name: str = "box"


def test_the_unresolvable_annotation_really_cannot_be_resolved():
    """The premise: without it the fallback below is never reached."""
    from typing import get_type_hints

    with pytest.raises(NameError):
        get_type_hints(_Opts)


def test_secret_fields_are_found_beside_an_unresolvable_annotation():
    """Mutation: the fallback returns ``{}`` and neither secret is found."""
    assert sensitive_field_names(_Opts) == {"token", "pw"}


def test_the_dry_run_renders_an_options_class_with_an_unresolvable_annotation():
    line = _options_line(_Opts(x=None, pw="hunter2", name="box"))  # ty: ignore[invalid-argument-type]
    assert line == "_Opts: token=<hidden>, x=None, pw=<hidden>, name='box'"
    assert "hunter2" not in line


Password = SecretStr
"""A RUNTIME alias: evaluable, so its field is found by type, not by text."""


@dataclasses.dataclass
class _Aliased:
    x: "Fraction | None" = None
    b: "Password | None" = None


@dataclasses.dataclass
class _BadAttribute:
    y: "os.Nope" = None  # ty: ignore[unresolved-attribute]
    pw: SecretStr | None = None


@dataclasses.dataclass
class _Generic:
    s: Secret[str] | None = None
    name: str = "box"


def test_a_runtime_alias_is_resolved_beside_an_unresolvable_sibling():
    """Each string is evaluated on its own: a sibling that fails demotes nothing.

    Mutation: judge every string by its text once one fails, and ``b``
    (whose text names no secret type) is missed.
    """
    assert sensitive_field_names(_Aliased) == {"b"}


def test_an_annotation_that_raises_attribute_error_falls_back_too():
    """``"os.Nope"`` raises ``AttributeError``, not ``NameError``; the fallback still runs."""
    from typing import get_type_hints

    with pytest.raises(AttributeError):
        get_type_hints(_BadAttribute)
    assert sensitive_field_names(_BadAttribute) == {"pw"}


def test_a_subscripted_secret_field_is_found_and_hidden():
    """``Secret[str]`` is a secret by its origin; its value never reaches the dry-run echo."""
    assert sensitive_field_names(_Generic) == {"s"}
    line = _options_line(_Generic(s="hunter3"))  # ty: ignore[invalid-argument-type]
    assert line == "_Generic: s=<hidden>, name='box'"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("SecretStr", True),
        ("pydantic.SecretStr", True),
        ("Optional[SecretStr]", True),
        ("list[SecretBytes] | None", True),
        ("Secret[str] | None", True),
        ("SecretKey", False),
        ("NotSecretStr", False),
        ("SecretStrength", False),
        ("SSHClientConnection", False),
        # The documented limit: an alias imported only for type checking
        # does not name the type, so its text cannot reveal it.
        ("Password | None", False),
    ],
)
def test_an_unresolved_annotation_is_secret_when_its_text_names_the_type(text, secret):
    assert contains_secret_type(text) is secret
    assert contains_secret_type(ForwardRef(text)) is secret
