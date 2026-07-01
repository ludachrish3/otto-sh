"""verb_creates_output_dir reflects each verb's @cli_exposed(output_dir=...) flag."""

from otto.cli.expose import verb_creates_output_dir


def test_read_only_verbs_create_no_dir() -> None:
    for verb in ("exists", "ls", "read-file", "is-installed", "is-uninstalled", "lsmod"):
        assert verb_creates_output_dir(verb) is False, verb


def test_work_verbs_create_dir() -> None:
    for verb in ("run", "get", "put", "login"):
        assert verb_creates_output_dir(verb) is True, verb


def test_unknown_verb_defaults_true() -> None:
    assert verb_creates_output_dir("no-such-verb") is True


def test_read_only_flag_consistent_across_host_classes() -> None:
    """verb_creates_output_dir is global (first-registration wins), so every host
    class that exposes a read-only verb must declare the SAME output_dir flag —
    otherwise the answer would depend on _HOST_CLASSES registration order."""
    import inspect

    from otto.host.embedded_host import EmbeddedHost
    from otto.host.unix_host import UnixHost

    for cls in (UnixHost, EmbeddedHost):
        for verb in ("exists", "ls"):
            fn = inspect.getattr_static(cls, verb, None)
            if fn is None or not getattr(fn, "__cli_exposed__", False):
                continue
            assert getattr(fn, "__cli_output_dir__", True) is False, f"{cls.__name__}.{verb}"
