"""The generated VS Code snippets track the live boundary models (spec §12)."""

import json

from otto.models.snippets import build_snippets


def test_snippets_cover_the_v2_shapes() -> None:
    snips = build_snippets(builtins_only=True)
    assert {
        "otto lab entry",
        "otto element",
        "otto unix host",
        "otto embedded host",
        "otto cred",
    } <= set(snips)
    unix = snips["otto unix host"]
    assert unix["prefix"] == "otto-unix-host"
    body = "\n".join(unix["body"])
    assert '"ip"' in body
    assert '"creds"' in body
    assert '"os_type": "unix"' in body
    # `labs` is hoisted, so it can never appear in a host entry.
    #
    # `resources` MAY appear in one since spec 2026-08-28
    # three-level-reservations §2 — leaving it out of the snippet is a
    # deliberate choice, not a consequence of the rule. `lab-config.md` lists
    # it as an optional host field, but a reservation identifier names a lock
    # in someone's booking system: there is no placeholder a reader could fill
    # by looking at the rest of the file, which is the bar `_COMMON_OPTIONAL`
    # sets. It belongs in the schema's autocomplete, like `hop`, not in the
    # starting skeleton. Pinned so re-adding it has to be a decision.
    assert '"labs"' not in body
    assert '"resources"' not in body


def test_required_fields_come_from_the_live_model() -> None:
    from otto.models.host import UnixHostSpec
    from otto.models.lab import HOISTED_HOST_KEYS

    body = "\n".join(build_snippets(builtins_only=True)["otto unix host"]["body"])
    for name, f in UnixHostSpec.model_fields.items():
        if f.is_required() and name not in HOISTED_HOST_KEYS:
            assert f'"{name}"' in body
    assert '"element"' not in body  # required on the flat spec, but hoisted in the file


def test_every_snippet_has_the_fields_vscode_needs() -> None:
    for name, snip in build_snippets(builtins_only=True).items():
        assert snip["scope"] == "json", name
        assert snip["prefix"].startswith("otto-"), name
        assert snip["description"], name
        assert isinstance(snip["body"], list), name
        assert snip["body"], name


def test_host_body_is_valid_json_once_the_placeholders_are_filled() -> None:
    """A snippet the user tabs straight through must still parse as JSON."""
    import re

    for key in ("otto unix host", "otto embedded host", "otto element", "otto cred"):
        body = "\n".join(build_snippets(builtins_only=True)[key]["body"])
        filled = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", body).replace("$0", "")
        json.loads(filled)  # raises if the generated body is not well-formed


def test_valid_terms_default_comes_from_the_model_not_a_literal() -> None:
    from otto.models.host import UnixHostSpec

    body = "\n".join(build_snippets(builtins_only=True)["otto unix host"]["body"])
    default = UnixHostSpec.model_fields["valid_terms"].get_default(call_default_factory=True)
    rendered = ", ".join(f'"{v}"' for v in default)
    assert f'"valid_terms": [{rendered}]' in body


def test_embedded_snippet_declares_its_own_os_type() -> None:
    """``zephyr`` shares ``EmbeddedHostSpec``; one snippet per SPEC, not per name."""
    snips = build_snippets(builtins_only=True)
    assert "otto zephyr host" not in snips
    body = "\n".join(snips["otto embedded host"]["body"])
    assert '"os_type": "embedded"' in body
