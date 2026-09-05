"""Every parse rule of spec §4.3, against a hand-built tree."""

from pathlib import Path

import pytest

from otto import _shim_complete as sc


def _param(flags, name, *, takes_value=True, multiple=False, nargs=1, source=None, sep=None):
    return {
        "flags": flags,
        "name": name,
        "takes_value": takes_value,
        "multiple": multiple,
        "nargs": nargs,
        "sep": sep,
        "source": source or {"kind": "none"},
    }


def _leaf(name, params=()):
    """A TyperCommand node: `group` False is click's allow_interspersed_args=True."""
    return {"name": name, "params": list(params), "commands": {}, "group": False}


def _tree():
    hosts = {
        "kind": "payload",
        "key": "hosts",
        "lab_scoped": True,
        "sort": True,
        "always": ["local"],
    }
    # The host verbs are shared node dicts: the union menu and each class view
    # point at the same objects, as the serialiser's views do (Task 6).
    run = _leaf("run", [_param([], "cmd", nargs=-1, source={"kind": "none"})])
    lsmod = _leaf("lsmod")
    put = _leaf(
        "put",
        [
            _param([], "files", nargs=-1, source={"kind": "none"}),
            _param([], "dest", source={"kind": "live"}),
        ],
    )
    reboot = _leaf("reboot")
    return {
        "name": "otto",
        # `group` mirrors isinstance(cmd, TyperGroup) — the ONE flag that decides
        # whether an option-looking word after a positional is parsed as an option
        # (TyperGroup.allow_interspersed_args is False).
        "group": True,
        "params": [
            _param(
                ["--lab", "-l"],
                "labs",
                multiple=True,
                sep="+",
                source={"kind": "payload", "key": "labs", "sort": True, "sep": "+"},
            ),
            _param(["--field", "--debug"], "debug", takes_value=False),
            _param(
                ["--xdir", "-x"], "xdir", source={"kind": "none"}
            ),  # a TyperPath: Typer offers nothing
            _param(
                ["--out", "-o"], "out", source={"kind": "echo"}
            ),  # a File: Typer echoes the fragment
            _param(["--help", "-h"], "help", takes_value=False),
        ],
        "commands": {
            "run": {
                "name": "run",
                "group": True,
                "params": [_param(["--help"], "help", takes_value=False)],
                "commands": {
                    "deploy": _leaf(
                        "deploy",
                        [
                            _param(["--count"], "count"),
                            _param(
                                ["--mode"],
                                "mode",
                                source={
                                    "kind": "static",
                                    "values": ["fast", "full"],
                                    "case_sensitive": True,
                                },
                            ),
                        ],
                    )
                },
            },
            "host": {
                "name": "host",
                "group": True,
                "scoped_by": "host_id",
                "params": [
                    _param([], "host_id", source=hosts),
                    _param(
                        ["--term"],
                        "term",
                        source={"kind": "payload", "key": "term_backends", "sort": True},
                    ),
                    # nargs>1 on an OPTION: click pops two words for it and looks two
                    # words back for its pending rule; the shim models one, so it hands
                    # over. On the host GROUP, so the walk, the `=` split and the
                    # textual pending rule (after the group's positional) all reach it.
                    _param(["--pair"], "pair", nargs=2),
                ],
                "commands": {"run": run, "lsmod": lsmod, "put": put, "reboot": reboot},
            },
            "test": _leaf(
                "test",
                [
                    _param(["--tests"], "tests", sep=",", source={"kind": "tests", "sep": ","}),
                    _param(["--markers", "-m"], "markers", source={"kind": "markers"}),
                ],
            ),
            "tunnel": {
                "name": "tunnel",
                "group": True,
                "params": [],
                "commands": {
                    "add": _leaf(
                        "add",
                        [
                            _param(
                                ["--hosts"],
                                "hosts",
                                sep=",",
                                source={**hosts, "sep": ",", "live_past_sep": True},
                            )
                        ],
                    ),
                    "remove": _leaf("remove", [_param([], "tunnel_id", source={"kind": "live"})]),
                },
            },
            "pair": _leaf(
                "pair", [_param([], "ab", nargs=2)]
            ),  # nargs>1 is not modelled: hand over
        },
        "host_classes": {
            "unix": {"run": run, "lsmod": lsmod, "put": put},
            "zephyr": {"run": run, "reboot": reboot},
        },
    }


NAMES = {
    "hosts": ["a1", "local", "z1"],
    "hosts_by_lab": {"east": ["a1"], "west": ["z1"]},
    "labs": ["east", "west"],
    "term_backends": ["ssh", "telnet"],
    "host_classes_by_id": {"z1": "zephyr", "a1": "unix"},
}
TESTS = {"tests": ["test_a", "test_b"], "markers": ["slow", "smoke"]}
COLLECTED = {"names": ["test_gen"], "markers": ["deep"]}
ROOT_OPTIONS = ["--lab", "-l", "--field", "--debug", "--xdir", "-x", "--out", "-o", "--help", "-h"]


def _answer(words: str, cword: int, env: dict | None = None, collected=COLLECTED):
    parts = sc.split_arg_string(words)
    args, frag = parts[1:cword], (parts[cword] if cword < len(parts) else "")
    tree = _tree()
    res = sc.resolve(tree, args, NAMES["host_classes_by_id"])
    return sc.complete(tree, res, frag, env or {}, sc.Payloads(NAMES, TESTS, collected))


def test_click_split_keeps_a_partial_quoted_token():
    assert sc.split_arg_string('otto run "te') == ["otto", "run", "te"]
    assert sc.split_arg_string("otto my\\") == [
        "otto",
        "my",
    ]  # an incomplete escape is dropped: click keeps the partial token as shlex left it (measured)


def test_subcommands_then_options_by_fragment_kind():
    assert _answer("otto ", 1) == ["run", "host", "test", "tunnel", "pair"]
    assert _answer("otto -", 1) == ROOT_OPTIONS
    assert _answer("otto --l", 1) == ["--lab"]
    assert _answer("otto r", 1) == ["run"]


def test_given_non_multiple_options_are_not_offered_again():
    assert "--xdir" not in _answer("otto --xdir /tmp -", 3)
    assert "--field" not in _answer("otto --debug -", 2)
    assert "--lab" in _answer("otto -l east -", 3)
    assert (
        _answer("otto -least -", 2) == ROOT_OPTIONS
    )  # `-svalue` gives --lab a value; it is multiple


def test_pending_value_option_completes_its_source():
    assert _answer("otto --xdir ", 2) == []  # TyperPath: nothing, not the fragment
    assert _answer("otto --xdir /tm", 2) == []
    assert _answer("otto --out ", 2) == [""]  # File: the fragment itself
    assert _answer("otto --out /tm", 2) == ["/tm"]
    assert _answer("otto -l ", 2) == ["east", "west"]
    assert _answer("otto -l east+w", 2) == ["east+west"]
    assert _answer("otto --lab=e", 1) == ["east"]
    assert _answer("otto --lab=", 1) == ["east", "west"]
    assert _answer("otto -l=e", 1) == [
        "east"
    ]  # click splits the FRAGMENT on `=` for short options too
    # … and then RE-APPLIES its option-name rule to the value part: `_resolve_incomplete`
    # appends the name to `args` and falls through, so a `-` value completes option NAMES
    assert _answer("otto --lab=-", 1) == ROOT_OPTIONS
    assert _answer("otto -l=-", 1) == ROOT_OPTIONS
    assert _answer("otto --lab=--l", 1) == ["--lab"]
    assert _answer("otto --xdir=-", 1) == ROOT_OPTIONS  # the flag's own value is irrelevant
    assert _answer("otto --debug=-", 1) == ROOT_OPTIONS  # … and so is its taking no value


def test_a_dash_fragment_is_an_option_name_before_any_pending_value():
    # click's option-name rule runs BEFORE its pending-option rule, and an option
    # whose value has not arrived is not yet "given" (no ParameterSource.COMMANDLINE)
    assert _answer("otto --xdir -", 2) == ROOT_OPTIONS
    assert _answer("otto test -m", 2) == ["-m"]  # `-m` is a prefix of exactly one flag


def test_the_pending_option_is_the_last_word_read_textually():
    # click's _is_incomplete_option looks at args[-1] as TEXT: the parser took `--lab`
    # as --xdir's value, yet the completer completes --lab's values
    assert _answer("otto --xdir --lab ", 3) == ["east", "west"]
    assert _answer("otto --xdir --lab e", 3) == ["east"]
    assert "--xdir" not in _answer("otto --xdir --lab -", 3)  # --xdir DID receive a value: `--lab`
    # `--flag=val` puts the flag last for that rule: a flag takes no value, so the
    # fragment `x` falls through to the command menu (nothing starts with "x")
    assert _answer("otto --xdir --debug=x", 2) == []


def test_static_choice_filters_by_prefix():
    assert _answer("otto run deploy --mode f", 4) == ["fast", "full"]
    assert _answer("otto run deploy --mode fu", 4) == ["full"]


def test_double_dash_is_global_for_the_fragment_but_per_parser_for_the_walk():
    # after `--` a `-x` fragment is a positional value for the variadic `cmd`
    assert _answer("otto host a1 run -- -", 5) == []
    # `--` at the root is seen by the fragment rule ("--" in the WHOLE line): no option names …
    assert _answer("otto -- host -", 3) == []  # host_id candidates filtered by "-": none
    # … while the child's parser still parses its own options
    assert _answer("otto -- host a1 --term ", 5) == ["ssh", "telnet"]
    # with nothing positional left to complete, click falls to the command's
    # own menu, which lists option names for a non-alphanumeric fragment
    assert _answer("otto -- -", 2) == ROOT_OPTIONS


def test_host_positional_then_class_scoped_verbs():
    assert _answer("otto host ", 2) == ["a1", "local", "z1"]
    assert _answer("otto -l east host ", 4) == ["a1", "local"]
    assert _answer("otto host z1 ", 3) == ["run", "reboot"]
    assert _answer("otto host a1 ", 3) == ["run", "lsmod", "put"]
    assert _answer("otto host ghost ", 3) == ["run", "lsmod", "put", "reboot"]
    assert _answer("otto host z1 -", 3) == ["--term", "--pair"]
    assert _answer("otto host a1 --term ", 4) == ["ssh", "telnet"]
    assert (
        _answer("otto host a1 put x y ", 6) == []
    )  # the variadic (a Path) wins and offers nothing
    assert _answer("otto host a1 put x y /e", 6) == []


def test_lab_from_env_when_no_flag_is_given():
    assert _answer("otto host ", 2, {"OTTO_LAB": "west"}) == ["local", "z1"]
    assert _answer("otto host ", 2, {"OTTO_LAB": "west east"}) == ["a1", "local", "z1"]
    assert _answer("otto host ", 2, {"OTTO_LAB": ""}) == ["a1", "local", "z1"]
    assert _answer("otto -l east host ", 4, {"OTTO_LAB": "west"}) == ["a1", "local"]
    assert _answer("otto -l east+ host ", 4, {"OTTO_LAB": "west"}) == [
        "a1",
        "local",
        "z1",
    ]  # malformed flag → no lab, no env fallback


def test_tests_and_markers_sites():
    assert _answer("otto test --tests ", 3) == ["test_a", "test_b", "test_gen"]
    assert _answer("otto test --tests test_a,te", 3) == ["test_a,test_b", "test_a,test_gen"]
    assert _answer('otto test -m "smoke and "', 3) == [
        "smoke and deep",
        "smoke and slow",
        "smoke and smoke",
    ]
    assert _answer("otto test -m 'not (s", 3) == [
        "not (slow",
        "not (smoke",
    ]  # unterminated quote: partial token


def test_hand_overs():
    with pytest.raises(sc.Handover, match="unknown option"):
        _answer("otto --bogus ", 2)
    with pytest.raises(sc.Handover, match="unknown option"):
        _answer("otto --la east host ", 4)  # click matches NO long-option prefix; never guess
    with pytest.raises(sc.Handover, match="unknown option"):
        _answer("otto --nope=e", 1)
    with pytest.raises(sc.Handover, match="stacked"):
        _answer("otto -hx ", 2)  # click stacks short flags; the shim does not model it
    with pytest.raises(sc.Handover, match="unknown command"):
        _answer("otto nope ", 2)
    with pytest.raises(sc.Handover, match="live"):
        _answer("otto tunnel remove ", 3)
    with pytest.raises(sc.Handover, match="past its first"):
        _answer("otto tunnel add --hosts a1,", 4)
    with pytest.raises(sc.Handover, match="collected"):
        _answer("otto test --tests ", 3, collected=None)
    with pytest.raises(sc.Handover, match="nargs=2 positional"):
        _answer("otto pair ", 2)
    with pytest.raises(sc.Handover, match="nargs=2 positional"):
        _answer("otto pair x ", 3)
    # a value attached to a no-value flag: click raises BadOptionUsage and resilient
    # parsing abandons the whole parse of that command, dropping every later word
    with pytest.raises(sc.Handover, match="value given to flag"):
        _answer("otto --debug=x host ", 4)
    with pytest.raises(sc.Handover, match="value given to flag"):
        _answer("otto --xdir /tmp --debug= host ", 6)
    with pytest.raises(sc.Handover, match="nargs=2 option"):
        _answer("otto host --pair a b ", 6)  # the walk
    with pytest.raises(sc.Handover, match="nargs=2 option"):
        _answer("otto host --pair=a", 2)  # the `=` fragment split
    with pytest.raises(sc.Handover, match="nargs=2 option"):
        _answer("otto host z1 --pair ", 4)  # the textual pending rule


def test_a_required_node_key_is_indexed_never_guessed():
    """``name`` is a REQUIRED Node key, like ``group``, ``params`` and ``commands``.

    Read with ``.get`` a payload written without it would quietly stop recognising
    the ROOT ``--lab`` (every node's name reading ``None``), so host completion
    would ignore the flag instead of handing over; indexed, it raises and
    ``answer_or_reason`` hands over.
    """
    tree = _tree()
    del tree["name"]
    with pytest.raises(KeyError):
        sc.resolve(tree, ["-l", "east", "host"], NAMES["host_classes_by_id"])


def test_site_of():
    assert sc.site_of({"kind": "tests"}) == "tests"
    assert sc.site_of({"kind": "markers"}) == "tests"
    assert sc.site_of({"kind": "payload", "key": "hosts"}) == "names"


def test_workspace_key_matches_the_product(tmp_path: Path, monkeypatch):
    from otto.config.home import workspace_key

    real = tmp_path / "Real_Repo.x"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)

    # Two genuinely distinct sibling paths whose sort order a plain string sort
    # gets wrong: as strings "a-b" < "a/b" (`-` is 0x2D, `/` is 0x2F), but
    # pathlib's component-wise compare puts "a" < "a-b", so "a/..." sorts
    # first. Same shape with `.`.
    a_b = tmp_path / "a" / "b"
    a_b.mkdir(parents=True)
    a_dash_b_x = tmp_path / "a-b" / "x"
    a_dash_b_x.mkdir(parents=True)
    app_core = tmp_path / "app.core"
    app_core.mkdir()
    app_web = tmp_path / "app" / "web"
    app_web.mkdir(parents=True)

    corpora = [
        [str(real)],
        [str(link), str(real)],
        ["Real_Repo.x", str(real)],
        [],
        [str(a_b), str(a_dash_b_x)],
        [str(a_dash_b_x), str(a_b)],
        [str(app_core), str(app_web)],
        [str(app_web), str(app_core)],
    ]
    for corpus in corpora:
        assert sc.workspace_key(corpus) == workspace_key([Path(p) for p in corpus])
