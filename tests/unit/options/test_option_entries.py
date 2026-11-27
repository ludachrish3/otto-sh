"""``verbs=`` entries name commands; which registrations cover a command, and resolving one."""

import sys

import pytest

import otto
from otto.params import (
    OptionsRegistrationError,
    Registration,
    covering,
    entry_covers,
    register_options,
    registrations,
    resolve_registration,
    verb_option_classes,
    verbs_for,
)
from otto.registry import registering_project


@pytest.mark.parametrize(
    ("entry", "path", "covers"),
    [
        ("run", ("run", "install"), True),
        ("run install", ("run", "install"), True),
        ("run install", ("run", "status"), False),
        ("run", ("runner",), False),
        ("flash", ("flash", "erase", "all"), True),
        ("flash erase", ("flash", "erase", "all"), True),
        ("flash", ("flash",), True),
        ("test", ("run", "test"), False),
    ],
)
def test_an_entry_covers_the_paths_it_prefixes(entry, path, covers):
    assert entry_covers(entry, path) is covers


@pytest.mark.parametrize(
    "verbs",
    [
        "run",
        [],
        [""],
        ["Run "],
        [" run"],
        ["run  install"],
        ["run install now"],
        ["run", "run"],
        ["run", "run install"],
        ["run install", "run"],
        [3],
    ],
)
def test_a_malformed_list_is_refused_at_registration(verbs):
    with pytest.raises(OptionsRegistrationError) as e:
        otto.options(verbs=verbs)(type("C", (), {}))
    assert "verbs" in str(e.value)


@pytest.mark.parametrize("entry", ["", "Run ", " run", "run  install", "run install now"])
def test_a_mistyped_entry_is_shown_with_the_accepted_form(entry):
    with pytest.raises(OptionsRegistrationError) as e:
        otto.options(verbs=[entry])(type("C", (), {}))
    assert repr(entry) in str(e.value)
    assert '"run install"' in str(e.value)


def test_a_first_word_no_command_has_is_not_judged_at_registration():
    @otto.options(verbs=["tset"])
    class C:
        x: int = 0

    assert verbs_for(C) == ["tset"]


def test_coverage_resolves_nothing(tmp_path, monkeypatch):
    pkg = tmp_path / "lazy_mod"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from otto import options\n@options\nclass Cls:\n    x: int = 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "lazy_mod", raising=False)
    register_options("lazy_mod:Cls", verbs=["run install"])
    assert [r.key for r in covering(("run", "install"))] == ["lazy_mod:Cls"]
    assert "lazy_mod" not in sys.modules
    assert covering(("run", "status")) == []


@otto.options
class A:
    a: int = 0


@otto.options
class B:
    b: int = 0


@otto.options
class C:
    c: int = 0


def test_coverage_keeps_the_projects_it_is_handed_and_registrations_nobody_owns():
    with registering_project("a"):
        register_options(A, verbs=["run"])
    with registering_project("b"):
        register_options(B, verbs=["run"])
    register_options(C, verbs=["run"])
    path = ("run", "install")
    keys = [otto.params.options_key(cls) for cls in (A, B, C)]
    assert [r.key for r in covering(path, kept={"a"})] == [keys[0], keys[2]]
    assert [r.key for r in covering(path)] == keys
    assert [r.key for r in covering(path, kept=())] == [keys[2]]
    assert [r.project for r in covering(path)] == ["a", "b", None]


def test_coverage_under_host_is_empty_until_host_commands_take_flags():
    register_options(A, verbs=["host install"])
    assert covering(("host", "install")) == []


def test_coverage_over_handed_registrations_asks_no_registry():
    handed = [
        Registration(key="nowhere:One", entries=("flash",), project=None),
        Registration(key="nowhere:Two", entries=("run",), project="a"),
        Registration(key="nowhere:Three", entries=("flash erase",), project="b"),
    ]
    assert [r.key for r in covering(("flash", "erase"), among=handed, kept={"b"})] == [
        "nowhere:One",
        "nowhere:Three",
    ]
    assert registrations() == []


def test_registrations_are_the_registry_as_registered():
    with registering_project("a"):
        register_options(A, verbs=["run install", "test"])
    assert registrations() == [
        Registration(key=otto.params.options_key(A), entries=("run install", "test"), project="a")
    ]


def test_a_registration_that_cannot_be_imported_names_itself_and_its_project():
    with registering_project("a"):
        register_options("nomod:Cls", verbs=["run"])
    (registration,) = registrations()
    with pytest.raises(
        OptionsRegistrationError, match=r"nomod:Cls.*project 'a'.*cannot be imported"
    ) as e:
        resolve_registration(registration)
    assert isinstance(e.value.__cause__, ImportError)


def test_resolving_a_registration_gives_its_class_and_project():
    with registering_project("a"):
        register_options(A, verbs=["run"])
    (registration,) = registrations()
    origin = resolve_registration(registration)
    assert (origin.cls, origin.project) == (A, "a")


def test_verb_option_classes_is_the_group_only():
    register_options(A, verbs=["run install"])
    register_options(B, verbs=["run"])
    assert [o.cls for o in verb_option_classes("run")] == [B]


def test_a_library_process_installs_no_checker_and_judges_no_first_word():
    import subprocess

    script = (
        "import sys\n"
        "import otto\n"
        "import otto.params as p\n"
        "@otto.options(verbs=['tset'])\n"
        "class C:\n"
        "    x: int = 0\n"
        "otto.register_options('nomod:Cls', verbs=['lnik nope'])\n"
        "assert p.verbs_for(C) == ['tset']\n"
        "assert p._ENTRY_CHECKER.check is None\n"
        "assert 'otto.cli.registry' not in sys.modules, 'the command line was loaded'\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
