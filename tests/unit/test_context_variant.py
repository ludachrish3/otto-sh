"""The run's product variant: one ContextVar, debug unless the root flag says field."""

import pytest

from otto import context


def test_the_default_variant_is_debug():
    assert context.variant() == "debug"


def test_set_variant_is_read_back_and_reset_by_its_token():
    token = context.set_variant("field")
    assert context.variant() == "field"
    context._variant.reset(token)
    assert context.variant() == "debug"


def test_set_variant_refuses_anything_but_the_two_names():
    with pytest.raises(ValueError, match=r"variant must be 'debug' or 'field', got 'release'"):
        context.set_variant("release")  # type: ignore[arg-type]


def test_the_cli_variant_is_undone_with_the_cli_context():
    context.set_cli_variant("field")
    assert context.variant() == "field"
    context.reset_cli_context()
    assert context.variant() == "debug"


def test_reset_cli_context_without_a_cli_variant_is_a_noop():
    context.reset_cli_context()
    assert context.variant() == "debug"


def test_variants_is_the_public_pair():
    assert context.VARIANTS == ("debug", "field")
