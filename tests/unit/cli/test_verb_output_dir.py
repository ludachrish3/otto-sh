"""Cross-class consistency of ``@cli_exposed(output_dir=...)`` markers.

The leaf-invoke preamble reads ``__cli_output_dir__`` straight from the
resolved verb callback of whichever host class serves the invocation. A verb
that declared different flags on different classes would therefore behave
differently depending on ``--host`` — which no verb intends. These tests pin
the declared markers and their cross-class consistency. (The old
``expose.verb_creates_output_dir()`` helper that answered this globally is
gone: production reads the marker directly off the callback.)
"""

import inspect

from otto.cli.expose import collect_exposed_methods, iter_exposed_verbs


def _declared_flags() -> dict[str, bool]:
    """cli_name -> declared ``__cli_output_dir__`` (first registration wins)."""
    return {
        cli_name: bool(getattr(fn, "__cli_output_dir__", True))
        for cli_name, _attr, _help, fn in iter_exposed_verbs()
    }


def test_read_only_verbs_declare_no_dir() -> None:
    flags = _declared_flags()
    for verb in ("exists", "ls", "read-file", "is-installed", "is-uninstalled", "lsmod"):
        assert flags[verb] is False, verb


def test_work_verbs_declare_dir() -> None:
    flags = _declared_flags()
    for verb in ("run", "get", "put", "login"):
        assert flags[verb] is True, verb


def test_output_dir_flag_consistent_across_host_classes() -> None:
    """Every host class exposing a verb must declare the SAME output_dir flag.

    ``iter_exposed_verbs`` is first-registration-wins, so an inconsistent
    marker would make behavior depend on HOST_CLASSES registration order.
    """
    from otto.host.os_profile import HOST_CLASSES

    per_verb: dict[str, dict[str, bool]] = {}
    for cls_name, cls in HOST_CLASSES.items():
        for cli_name, attr_name in collect_exposed_methods(cls).items():
            fn = inspect.getattr_static(cls, attr_name, None) or getattr(cls, attr_name)
            flag = bool(getattr(fn, "__cli_output_dir__", True))
            per_verb.setdefault(cli_name, {})[cls_name] = flag
    for verb, per_class in sorted(per_verb.items()):
        assert len(set(per_class.values())) == 1, f"{verb} diverges across classes: {per_class}"
