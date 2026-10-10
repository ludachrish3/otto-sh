"""Three layers, one resolver, a whole-profile first hit (R §3.4)."""

import ast
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from otto.config.repo import Repo
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.os_profile import (
    BUILTIN_PROFILES,
    HOST_CLASSES,
    OS_PROFILES,
    UNIX_LOGIN_PROMPT,
    UNIX_PASSWORD_PROMPT,
    OsProfile,
    ProfileContext,
    ProfileFields,
    build_os_profile,
    check_data_profiles,
    register_host_class,
    register_os_profile,
    registered_profile_names,
    resolve_os_profile,
)
from otto.host.unix_host import UnixHost
from otto.registry import DuplicateRegistration, FrozenMap
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo

SRC = PROJECT_ROOT / "src" / "otto"


def _data(**profiles: OsProfile) -> ProfileContext:
    return ProfileContext(
        profiles=FrozenMap(profiles), owners=FrozenMap(dict.fromkeys(profiles, "repo-a"))
    )


def test_a_data_table_named_unix_replaces_the_whole_class_profile():
    data = _data(unix=OsProfile("unix", "unix", ProfileFields(FrozenMap({"has_bash": False}))))
    got = resolve_os_profile("unix", data=data)
    assert got.fields.login_prompt is None
    assert dict(got.fields.defaults) == {"has_bash": False}


def test_a_code_profile_beats_data_regardless_of_order():
    data = _data(mine=OsProfile("mine", "unix"))
    register_os_profile("mine", "unix", {"has_bash": True})
    assert dict(resolve_os_profile("mine", data=data).fields.defaults) == {"has_bash": True}


def test_registering_a_host_class_writes_no_profile():
    class Mine(UnixHost):
        pass

    register_host_class("mine-cls", Mine)
    assert "mine-cls" not in OS_PROFILES
    assert resolve_os_profile("mine-cls", data=ProfileContext.empty()).base == "mine-cls"


def test_a_data_table_named_after_a_class_registered_later_still_wins():
    data = _data(late=OsProfile("late", "unix", ProfileFields(FrozenMap({"has_bash": False}))))

    class Late(UnixHost):
        pass

    register_host_class("late", Late)
    assert dict(resolve_os_profile("late", data=data).fields.defaults) == {"has_bash": False}


def test_re_registering_a_host_class_or_profile_needs_overwrite():
    class A(UnixHost):
        pass

    register_host_class("dup", A)
    with pytest.raises(DuplicateRegistration):
        register_host_class("dup", A)
    register_os_profile("dupp", "unix")
    with pytest.raises(DuplicateRegistration):
        register_os_profile("dupp", "unix")


def test_shadowing_a_built_in_needs_no_overwrite_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        register_os_profile("busybox", "unix")
    assert "overriding built-in" in caplog.text
    assert "busybox" in BUILTIN_PROFILES


def test_a_data_only_name_resolves_only_through_its_context():
    data = _data(dataonly=OsProfile("dataonly", "unix"))
    assert build_os_profile("dataonly", data=data).base == "unix"
    with pytest.raises(ValueError, match="dataonly"):
        build_os_profile("dataonly")  # no data= → code, class and built-in layers only
    assert "dataonly" not in registered_profile_names()
    assert "dataonly" in registered_profile_names(data=data)


def test_spec_is_keyword_only():
    with pytest.raises(TypeError):
        register_host_class("pos", UnixHost, object)  # type: ignore[misc]


# --- repo data tables, checked after init -------------------------------------


def _repo(tmp_path: Path, name: str, body: str) -> Repo:
    """A real repo named *name* under *tmp_path* whose settings append *body*."""
    return Repo(sut_dir=make_sut_repo(tmp_path / name, name=name, extra=textwrap.dedent(body)))


def test_check_data_profiles_names_the_repo_and_table_for_an_unknown_base(tmp_path):
    repo = _repo(tmp_path, "repo-a", '[os_profiles.broken]\nbase = "windows"\n')
    with pytest.raises(ValueError, match=r"\[os_profiles\.broken\] in repo 'repo-a': .*base must"):
        check_data_profiles([repo])


def test_check_data_profiles_names_the_repo_and_table_for_an_unknown_defaults_key(tmp_path):
    repo = _repo(tmp_path, "repo-a", '[os_profiles.broken]\nbase = "unix"\nosTyp = "unix"\n')
    with pytest.raises(
        ValueError, match=r"\[os_profiles\.broken\] in repo 'repo-a': .*unknown default field"
    ):
        check_data_profiles([repo])


def test_check_data_profiles_twice_is_idempotent(tmp_path):
    repo = _repo(tmp_path, "repo-a", '[os_profiles.fine]\nbase = "unix"\nhas_bash = false\n')
    revisions = (HOST_CLASSES.revision, OS_PROFILES.revision)
    check_data_profiles([repo])
    check_data_profiles([repo])
    assert (HOST_CLASSES.revision, OS_PROFILES.revision) == revisions
    assert "fine" not in OS_PROFILES


def test_a_data_profile_over_a_custom_host_class_resolves(tmp_path):
    # Parsed BEFORE the class is registered, as settings are parsed before init.
    repo = _repo(
        tmp_path, "repo-a", '[os_profiles.mine-v1]\nbase = "custom-late"\nhas_bash = false\n'
    )

    class CustomLate(UnixHost):
        pass

    register_host_class("custom-late", CustomLate)  # the init import
    check_data_profiles([repo])
    profile = resolve_os_profile("mine-v1", data=ProfileContext.from_repos([repo]))
    assert profile.base == "custom-late"
    assert dict(profile.fields.defaults) == {"has_bash": False}


def test_the_resolver_checks_a_data_profile_it_selects():
    data = _data(bad=OsProfile("bad", "unix", ProfileFields(FrozenMap({"osTyp": "unix"}))))
    with pytest.raises(ValueError, match=r"\[os_profiles\.bad\] in repo 'repo-a'"):
        resolve_os_profile("bad", data=data)


def test_the_later_repo_wins_a_same_named_table(tmp_path):
    a = _repo(tmp_path, "repo-a", '[os_profiles.x]\nbase = "unix"\nhas_bash = true\n')
    b = _repo(tmp_path, "repo-b", '[os_profiles.x]\nbase = "unix"\nhas_bash = false\n')
    context = ProfileContext.from_repos([a, b])
    assert dict(context.profiles["x"].fields.defaults) == {"has_bash": False}
    assert context.owners["x"] == "repo-b"
    assert dict(ProfileContext.from_repos([b, a]).profiles["x"].fields.defaults) == {
        "has_bash": True
    }


_DIGEST_CONTEXT = (
    "from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields\n"
    "from otto.registry import FrozenMap\n"
    "defaults = {'has_bash': False, 'valid_transfers': ['scp']}\n"
    "fields = ProfileFields(FrozenMap.freeze_json(defaults))\n"
    "context = ProfileContext(\n"
    "    profiles=FrozenMap(\n"
    "        {'x': OsProfile('x', 'unix', fields), 'a': OsProfile('a', 'embedded')}\n"
    "    ),\n"
    "    owners=FrozenMap({'x': 'repo-a', 'a': 'repo-b'}),\n"
    ")\n"
)


def test_profile_context_is_hashable_and_its_digest_is_stable_across_processes():
    namespace: dict[str, object] = {}
    exec(_DIGEST_CONTEXT, namespace)  # noqa: S102 — the same source the child process runs
    context = namespace["context"]
    assert isinstance(context, ProfileContext)
    assert {context: 1}[context] == 1
    child = subprocess.run(
        [sys.executable, "-c", _DIGEST_CONTEXT + "print(context.digest())"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == context.digest() == _PINNED_DIGEST


_PINNED_DIGEST = "f1df610d12730d108bcf950163aa95e91051a4cdfbc373ebf38d426385d215c5"


# --- console prompts follow the profile the host was built from ---------------

_DATA_UNIX = OsProfile("unix", "unix", ProfileFields(FrozenMap({"has_bash": True})))


def _built_under_data_unix() -> UnixHost:
    host = create_host_from_dict(
        {
            "ip": "10.0.0.5",
            "creds": [{"login": "u", "password": "p"}],
            "valid_terms": ["ssh", "telnet", "console"],
            "console_options": {"server": "cs1", "port": 7001},
        },
        element=Element("ne"),
        profiles=_data(unix=_DATA_UNIX),
    )
    assert isinstance(host, UnixHost)
    return host


def _prompts(host: UnixHost) -> tuple[str | None, str | None]:
    options = host.connections.console_options
    return options.login_prompt, options.password_prompt


def test_a_rebuilt_unix_host_keeps_its_data_profile_prompts():
    host = _built_under_data_unix()
    assert _prompts(host) == (None, None)
    host.rebuild_connections()
    assert _prompts(host) == (None, None)


def test_a_directly_constructed_unix_host_gets_class_prompts():
    host = UnixHost(ip="10.0.0.6", creds=[], element=Element("ne"))
    assert _prompts(host) == (UNIX_LOGIN_PROMPT, UNIX_PASSWORD_PROMPT)


def test_the_construction_profile_does_not_leak_to_the_next_host():
    _built_under_data_unix()
    host = UnixHost(ip="10.0.0.6", creds=[], element=Element("ne"))
    assert _prompts(host) == (UNIX_LOGIN_PROMPT, UNIX_PASSWORD_PROMPT)


def test_a_host_of_another_os_type_built_inside_a_construction_resolves_its_own():
    """The construction profile applies only to a host of the selector it was set for."""
    from otto.host.os_profile import constructing_with

    with constructing_with("outer-os", ProfileFields()):
        host = UnixHost(ip="10.0.0.7", creds=[], element=Element("ne"))
    assert _prompts(host) == (UNIX_LOGIN_PROMPT, UNIX_PASSWORD_PROMPT)


def test_an_overrides_copy_keeps_the_data_profile_prompts():
    from otto.config.fleet import _apply_option_overrides

    host = _built_under_data_unix()
    copy = _apply_option_overrides(host, term="console")
    assert copy is not host
    assert _prompts(copy) == (None, None)


def test_a_survey_copy_keeps_the_data_profile_prompts():
    from otto.host.survey.login import host_copy_on_port

    host = _built_under_data_unix()
    copy = host_copy_on_port(host, "telnet", 2323, login=None)
    assert _prompts(copy) == (None, None)


def test_a_survey_console_copy_keeps_the_data_profile_prompts(monkeypatch):
    """The ``attempt_console_open`` copy goes through ``copy_host`` as well."""
    import asyncio

    from otto.host.survey import login as survey_login

    host = _built_under_data_unix()
    copies: list[UnixHost] = []

    def _capture(source, /, **changes):
        made = original(source, **changes)
        copies.append(made)
        return made

    original = survey_login.copy_host
    monkeypatch.setattr(survey_login, "copy_host", _capture)

    async def _no_telnet(self):
        raise OSError("offline")

    monkeypatch.setattr(type(host.connections), "telnet", _no_telnet)
    asyncio.run(survey_login.attempt_console_open(host, 2323, timeout=1.0))
    assert len(copies) == 1
    assert _prompts(copies[0]) == (None, None)


def _host_copies_outside_copy_host() -> list[str]:
    """Every ``replace(host, ...)``/``dataclasses.replace(host, ...)`` outside ``copy_host``."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        allowed: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "copy_host":
                allowed.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in allowed:
                continue
            func = node.func
            named = (isinstance(func, ast.Name) and func.id == "replace") or (
                isinstance(func, ast.Attribute)
                and func.attr == "replace"
                and isinstance(func.value, ast.Name)
                and func.value.id == "dataclasses"
            )
            first = node.args[0] if node.args else None
            if named and isinstance(first, ast.Name) and first.id == "host":
                found.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return found


def test_no_host_copy_bypasses_copy_host():
    assert _host_copies_outside_copy_host() == []


def test_settings_parse_does_not_import_os_profile(tmp_path):
    sut = make_sut_repo(tmp_path / "p", name="p", extra='[os_profiles.x]\nbase = "unix"\n')
    probe = textwrap.dedent(f"""
        import sys
        import tomli
        from pathlib import Path
        from otto.config.repo import compile_settings
        root = Path({str(sut)!r})
        data = tomli.loads((root / ".otto" / "settings.toml").read_text())
        compiled = compile_settings(data, root)
        assert "x" in compiled.os_profiles
        print("otto.host.os_profile" in sys.modules)
    """)
    child = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert child.stdout.strip() == "False", child.stderr
