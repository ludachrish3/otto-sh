"""``scripts/api_regen.py``: a commit's dump comes from that commit alone (dump spec §5)."""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from scripts import api_regen
from tests._fixtures.api_dump import write_tree
from tests._fixtures.gitrepo import TmpGitRepo

pytestmark = pytest.mark.interpreter_agnostic

MANIFEST = '[namespaces."otto"]\ntier = 1\nstability = "provisional"\n'


def test_child_environment_is_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    src = write_tree(
        tmp_path,
        {
            "otto/__init__.py": "import json, os, pathlib\n__all__ = []\n"
            "(pathlib.Path(__file__).parent / 'env.json')"
            ".write_text(json.dumps(sorted(os.environ)))\n"
        },
    )
    api_regen.run_child(api_regen.CurrentEnv().python(tmp_path), src, ["otto"])
    keys = set(json.loads((src / "otto" / "env.json").read_text()))
    assert "AWS_SECRET_ACCESS_KEY" not in keys
    assert "PYTHONPATH" not in keys
    assert keys <= {
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "LANG",
        "TZ",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "LC_CTYPE",  # some interpreters add it at startup
    }


def test_child_timeout_kills_it(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": "import time\ntime.sleep(60)\n"})
    with pytest.raises(api_regen.EnvError, match="timed out"):
        api_regen.run_child(api_regen.CurrentEnv().python(tmp_path), src, ["otto"], timeout=3)


def test_child_crash_is_an_environment_error(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": "import os\nos._exit(3)\n"})
    with pytest.raises(api_regen.EnvError, match="crashed"):
        api_regen.run_child(api_regen.CurrentEnv().python(tmp_path), src, ["otto"])


def test_child_without_a_report_is_an_environment_error(tmp_path):
    body = (
        "import os, sys\nsys.__stdout__.write('garbage\\n')\nsys.__stdout__.flush()\nos._exit(0)\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": body})
    with pytest.raises(api_regen.EnvError, match="no report"):
        api_regen.run_child(api_regen.CurrentEnv().python(tmp_path), src, ["otto"])


def _commit_tree(repo, body):
    repo.write("api/public.toml", MANIFEST)
    repo.write("src/otto/__init__.py", body)
    return repo.commit("c")


def test_generate_at_commit_reads_the_commit_not_the_worktree(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    sha = _commit_tree(repo, '__all__ = ["f"]\ndef f(a): pass\n')
    repo.write("src/otto/__init__.py", '__all__ = ["f"]\ndef f(a, b): pass\n')  # dirty, uncommitted
    out = api_regen.generate_at_commit(repo.root, sha, "api/public.toml", api_regen.CurrentEnv())
    assert out.refusals == []
    assert "call\totto:f\tsync\tPK:a:-\n" in out.text


def test_generate_at_commit_without_a_manifest_is_refused(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/otto/__init__.py", "__all__ = []\n")
    sha = repo.commit("c")
    out = api_regen.generate_at_commit(repo.root, sha, "api/public.toml", api_regen.CurrentEnv())
    assert out.text is None
    assert len(out.refusals) == 1
    assert out.refusals[0].startswith("manifest: [Errno 2] No such file or directory:")
    assert out.refusals[0].endswith("/api/public.toml'")


CR = chr(13)


def test_the_manifest_is_read_as_committed_bytes(tmp_path):
    # A lone CR is no TOML newline; a newline-translating read would repair it.
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / "api").mkdir()
    (repo.root / "api" / "public.toml").write_bytes(MANIFEST.replace("\n", CR).encode())
    repo.write("src/otto/__init__.py", "__all__ = []\n")
    sha = repo.commit("c")
    out = api_regen.generate_at_commit(repo.root, sha, "api/public.toml", api_regen.CurrentEnv())
    assert out.text is None
    assert out.refusals[0].startswith("manifest:"), out.refusals


def test_a_non_utf8_manifest_is_a_refusal_not_a_traceback(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / "api").mkdir()
    (repo.root / "api" / "public.toml").write_bytes(MANIFEST.encode() + b"# \xff\n")
    repo.write("src/otto/__init__.py", "__all__ = []\n")
    sha = repo.commit("c")
    out = api_regen.generate_at_commit(repo.root, sha, "api/public.toml", api_regen.CurrentEnv())
    assert out.text is None
    assert out.refusals[0].startswith("manifest:"), out.refusals
    assert "utf-8" in out.refusals[0]


def test_generate_worktree_reports_provenance_as_a_refusal(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "manifest.toml").write_text(MANIFEST)
    out = api_regen.generate_worktree(tmp_path, tmp_path / "manifest.toml", assume_dir=True)
    assert out.text is None
    assert any(r.startswith("provenance:") for r in out.refusals), out.refusals


def test_generate_worktree_renders_a_parseable_dump(tmp_path):
    write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["f"]\ndef f(): pass\n'})
    (tmp_path / "manifest.toml").write_text(MANIFEST)
    out = api_regen.generate_worktree(tmp_path, tmp_path / "manifest.toml")
    assert out.text == (
        "# api-snapshot v2\n# producer-schema 1\nname\totto:f\tfunction\ncall\totto:f\tsync\t-\n"
    )


def _uv_project(root):
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0"\nrequires-python = ">=3.10"\ndependencies = []\n'
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=root, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_uv_deps_env_builds_once_per_key_and_reuses_it(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    first = env.python(tree)
    assert first.exists()
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(
        api_regen.subprocess, "run", lambda *a, **k: calls.append(a) or real_run(*a, **k)
    )
    assert env.python(tree) == first
    assert not any("sync" in (a[0] if a else []) for a in calls)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_uv_deps_env_refuses_a_lock_that_disagrees_with_pyproject(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    text = (
        (tree / "pyproject.toml")
        .read_text()
        .replace("dependencies = []", 'dependencies = ["fx-missing"]')
    )
    (tree / "pyproject.toml").write_text(text)
    with pytest.raises(api_regen.EnvError, match="uv lock --check failed"):
        api_regen.UvDepsEnv(cache_root=tmp_path / "cache").python(tree)


def _write_project(tree, deps="[]", extra=""):
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0"\nrequires-python = ">=3.10"\n'
        f"dependencies = {deps}\n{extra}"
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_cache_key_covers_the_lock_and_every_dependency_section(tmp_path):
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    tree = tmp_path / "tree"
    _write_project(tree)
    (tree / "uv.lock").write_text("a")
    base = env._key(tree)
    (tree / "uv.lock").write_text("b")
    assert env._key(tree) != base
    (tree / "uv.lock").write_text("a")
    assert env._key(tree) == base
    _write_project(tree, extra='[dependency-groups]\ndev = ["x"]\n')
    assert env._key(tree) != base
    _write_project(tree)
    text = (tree / "pyproject.toml").read_text()
    (tree / "pyproject.toml").write_text(text.replace('version = "0"', 'version = "1"'))
    assert env._key(tree) != base


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_stale_lock_is_refused_even_when_a_cache_entry_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    env.python(tree)
    old_key = env._key(tree)
    text = (tree / "pyproject.toml").read_text()
    (tree / "pyproject.toml").write_text(
        text.replace("dependencies = []", 'dependencies = ["fx-missing"]')
    )
    monkeypatch.setattr(env, "_key", lambda _tree: old_key)
    with pytest.raises(api_regen.EnvError, match="uv lock --check failed"):
        env.python(tree)


def test_missing_uv_is_an_environment_error(tmp_path, monkeypatch):
    monkeypatch.setattr(api_regen, "_tool", lambda _name: str(tmp_path / "no-such-uv"))
    tree = tmp_path / "tree"
    _write_project(tree)
    (tree / "uv.lock").write_text("")
    with pytest.raises(api_regen.EnvError, match="uv not found"):
        api_regen.UvDepsEnv(cache_root=tmp_path / "cache").python(tree)


_BAD_RECORD_REPORT = '{"records": [1], "refusals": [], "provenance": [], "private": {}}'


@pytest.mark.parametrize("printed", ["123", "{}", _BAD_RECORD_REPORT])
def test_malformed_child_report_is_a_refusal(tmp_path, printed):
    body = (
        "import os, sys\n"
        f"sys.__stdout__.write({printed!r} + '\\n')\n"
        "sys.__stdout__.flush()\nos._exit(0)\n"
    )
    write_tree(tmp_path, {"otto/__init__.py": body})
    (tmp_path / "manifest.toml").write_text(MANIFEST)
    out = api_regen.generate_worktree(tmp_path, tmp_path / "manifest.toml")
    assert out.text is None
    assert "malformed" in out.refusals[0]


def test_unparseable_record_is_a_refusal_not_a_traceback():
    report = {"records": ["not a record"], "refusals": [], "provenance": [], "private": {}}
    out = api_regen._generated(report)
    assert out.text is None
    assert out.refusals


def _fake_sync(monkeypatch, raised):
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if "sync" in argv:
            Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True)
            raise raised
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(api_regen.subprocess, "run", fake_run)


def _sync_env(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    _write_project(tree)
    (tree / "uv.lock").write_text("")
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    monkeypatch.setattr(env, "_check_lock", lambda _tree: None)
    return env, tree


def test_sync_timeout_is_an_environment_error_and_cleans_up(tmp_path, monkeypatch):
    env, tree = _sync_env(tmp_path, monkeypatch)
    _fake_sync(monkeypatch, subprocess.TimeoutExpired("uv", 1))
    with pytest.raises(api_regen.EnvError, match="timed out"):
        env.python(tree)
    assert not list((tmp_path / "cache").iterdir())


def test_sync_interrupt_still_cleans_up_the_partial(tmp_path, monkeypatch):
    env, tree = _sync_env(tmp_path, monkeypatch)
    _fake_sync(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        env.python(tree)
    assert not list((tmp_path / "cache").iterdir())


@pytest.mark.timeout(60)
def test_archive_refuses_an_escaping_symlink_without_hanging(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / "src").mkdir()
    (repo.root / "src" / "a-link").symlink_to("/etc/passwd")
    repo.write("src/z-big", "x" * 4_000_000)
    sha = repo.commit("c")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(api_regen.EnvError, match="archive"):
        api_regen.archive_commit(repo.root, sha, dest)


def test_archive_ignores_repository_location_variables(tmp_path, monkeypatch):
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/f.txt", "hello")
    sha = repo.commit("c")
    other = TmpGitRepo(tmp_path / "other")
    other.write("src/g.txt", "no")
    other.commit("o")
    monkeypatch.setenv("GIT_DIR", str(other.root / ".git"))
    dest = tmp_path / "dest"
    dest.mkdir()
    api_regen.archive_commit(repo.root, sha, dest)
    assert (dest / "src" / "f.txt").read_text() == "hello"
    assert not (dest / "src" / "g.txt").exists()


def _files_under(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_archive_extracts_only_what_regeneration_reads(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    for rel in [
        "src/otto/__init__.py",
        "pyproject.toml",
        "uv.lock",
        "meta/surface.toml",
        "meta/other.toml",
        "api/public.toml",
        "tests/test_x.py",
        "docs/index.md",
        "scripts/tool.py",
        "README.md",
    ]:
        repo.write(rel, "x\n")
    sha = repo.commit("c")
    dest = tmp_path / "dest"
    dest.mkdir()
    api_regen.archive_commit(repo.root, sha, dest, "meta/surface.toml")
    assert _files_under(dest) == [
        "meta/surface.toml",
        "pyproject.toml",
        "src/otto/__init__.py",
        "uv.lock",
    ]


def test_archive_leaves_out_an_input_the_commit_lacks(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/otto/__init__.py", "x\n")
    repo.write("README.md", "x\n")
    sha = repo.commit("c")
    dest = tmp_path / "dest"
    dest.mkdir()
    api_regen.archive_commit(repo.root, sha, dest, "api/public.toml")
    assert _files_under(dest) == ["src/otto/__init__.py"]


def test_archive_of_a_commit_with_no_input_extracts_nothing(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("README.md", "x\n")
    sha = repo.commit("c")
    dest = tmp_path / "dest"
    dest.mkdir()
    api_regen.archive_commit(repo.root, sha, dest, "api/public.toml")
    assert _files_under(dest) == []


def test_archive_reads_a_manifest_path_literally(tmp_path):
    # A manifest path is a file name, never a glob over its neighbours.
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("api/p*.toml", "x\n")
    repo.write("api/public.toml", "x\n")
    repo.write("api/private.toml", "x\n")
    sha = repo.commit("c")
    dest = tmp_path / "dest"
    dest.mkdir()
    api_regen.archive_commit(repo.root, sha, dest, "api/p*.toml")
    assert _files_under(dest) == ["api/p*.toml"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_unusable_cache_entry_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    entry = tmp_path / "cache" / env._key(tree)
    (entry / "bin").mkdir(parents=True)
    (entry / "bin" / "python").symlink_to(tmp_path / "nowhere")
    python = env.python(tree)
    assert subprocess.run([str(python), "-c", "pass"], check=False).returncode == 0


@pytest.mark.timeout(30)
def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    body = (
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": body})
    with pytest.raises(api_regen.EnvError, match="timed out"):
        api_regen.run_child(api_regen.CurrentEnv().python(tmp_path), src, ["otto"], timeout=3)
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail("the grandchild survived the timeout")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_lock_check_uses_the_sync_interpreter_not_uv_s_own_pick(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    monkeypatch.setenv("UV_PYTHON", str(tmp_path / "no-such-python"))
    api_regen.UvDepsEnv(cache_root=tmp_path / "cache")._check_lock(tree)


@pytest.mark.parametrize("raised", [subprocess.TimeoutExpired("uv", 1), OSError("boom")])
def test_lock_check_failures_to_run_are_environment_errors(tmp_path, monkeypatch, raised):
    def fake_run(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(api_regen.subprocess, "run", fake_run)
    with pytest.raises(api_regen.EnvError, match="could not run"):
        api_regen.UvDepsEnv(cache_root=tmp_path / "cache")._check_lock(tmp_path)


# Every uv variable that picks the project, lock, environment or working
# directory uv acts on, or overrides `--locked` (`uv help sync`, `uv help lock`).
UV_TARGETS = {
    "VIRTUAL_ENV": "/elsewhere/venv",
    "UV_PROJECT": "/elsewhere/project",
    "UV_WORKING_DIR": "/elsewhere",
    "UV_PROJECT_ENVIRONMENT": "/elsewhere/env",
    "UV_LOCKED": "1",
    "UV_FROZEN": "1",
}
# Cache, index, network and offline settings: an air-gapped install needs them.
UV_KEPT = {
    "UV_CACHE_DIR": "/cache",
    "UV_INDEX_URL": "https://mirror.invalid/simple",
    "UV_DEFAULT_INDEX": "https://mirror.invalid/simple",
    "UV_EXTRA_INDEX_URL": "https://extra.invalid/simple",
    "UV_FIND_LINKS": "/wheels",
    "UV_OFFLINE": "1",
    "UV_NATIVE_TLS": "1",
    "UV_CONFIG_FILE": "/etc/uv.toml",
}


def test_uv_env_strips_exactly_the_target_variables(monkeypatch):
    for name, value in {**UV_TARGETS, **UV_KEPT}.items():
        monkeypatch.setenv(name, value)
    assert set(UV_TARGETS) == api_regen.UV_TARGET_VARS
    env = api_regen._uv_env()
    assert not set(UV_TARGETS) & set(env)
    assert {k: env[k] for k in UV_KEPT} == UV_KEPT


def test_inherited_uv_project_does_not_redirect_the_lock_check_or_the_sync(tmp_path, monkeypatch):
    for name, value in {**UV_TARGETS, **UV_KEPT}.items():
        monkeypatch.setenv(name, value)
    tree = tmp_path / "tree"
    _write_project(tree)
    (tree / "uv.lock").write_text("")
    seen = {}
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv[1:2] in (["lock"], ["sync"]):
            seen[argv[1]] = (kwargs["cwd"], kwargs["env"])
            if argv[1] == "sync":
                partial = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
                (partial / "bin").mkdir(parents=True)
                (partial / "bin" / "python").symlink_to(api_regen.sys.executable)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(api_regen, "_uv_version", lambda: "uv 0")
    monkeypatch.setattr(api_regen.subprocess, "run", fake_run)
    api_regen.UvDepsEnv(cache_root=tmp_path / "cache").python(tree)
    assert set(seen) == {"lock", "sync"}
    for command, (cwd, env) in seen.items():
        assert cwd == tree, command
        assert {k: env[k] for k in UV_KEPT} == UV_KEPT, command
        leaked = set(UV_TARGETS) & set(env)
        assert leaked == ({"UV_PROJECT_ENVIRONMENT"} if command == "sync" else set()), command
    partial = Path(seen["sync"][1]["UV_PROJECT_ENVIRONMENT"])
    assert partial.parent == tmp_path / "cache"
    assert ".partial-" in partial.name


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_an_inherited_uv_project_does_not_redirect_the_real_lock_check(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    tree = tmp_path / "tree"
    _uv_project(tree)
    elsewhere = tmp_path / "elsewhere"
    _uv_project(elsewhere)
    text = (elsewhere / "pyproject.toml").read_text()
    (elsewhere / "pyproject.toml").write_text(
        text.replace("dependencies = []", 'dependencies = ["fx-missing"]')
    )
    monkeypatch.setenv("UV_PROJECT", str(elsewhere))
    api_regen.UvDepsEnv(cache_root=tmp_path / "cache")._check_lock(tree)


def _path_dep_project(root):
    """A project whose lock locates its one dependency at the path ``dep``."""
    (root / "dep").mkdir(parents=True)
    (root / "dep" / "pyproject.toml").write_text(
        '[project]\nname = "fx-dep"\nversion = "1"\nrequires-python = ">=3.10"\n'
        'dependencies = []\n[build-system]\nrequires = ["uv_build"]\nbuild-backend = "uv_build"\n'
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["fx-dep"]\n[tool.uv.sources]\nfx-dep = { path = "dep" }\n'
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=root, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_a_lock_that_no_longer_resolves_is_refused_even_on_a_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    tree = tmp_path / "tree"
    _path_dep_project(tree)
    env = api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    env._check_lock(tree)  # resolves while its source exists
    shutil.rmtree(tree / "dep")  # the locked source is gone, like a yanked package
    entry = tmp_path / "cache" / env._key(tree) / "bin"
    entry.mkdir(parents=True)
    (entry / "python").symlink_to(api_regen.sys.executable)
    with pytest.raises(api_regen.EnvError, match="uv lock --check failed"):
        env.python(tree)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_a_path_dependency_outside_the_archived_inputs_is_an_environment_refusal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    repo = TmpGitRepo(tmp_path / "repo")
    _path_dep_project(repo.root)
    repo.write("api/public.toml", MANIFEST)
    repo.write("src/otto/__init__.py", "__all__ = []\n")
    sha = repo.commit("c")
    out = api_regen.generate_at_commit(
        repo.root, sha, "api/public.toml", api_regen.UvDepsEnv(cache_root=tmp_path / "cache")
    )
    assert out.text is None
    assert len(out.refusals) == 1
    assert out.refusals[0].startswith("environment: uv lock --check failed"), out.refusals
