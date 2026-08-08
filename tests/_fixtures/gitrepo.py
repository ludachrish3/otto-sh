"""THE hermetic git harness for suite code (review §7.2).

Every git the suite spawns must run with an environment built here — the
drift guard in ``tests/unit/test_gitenv_hermeticity.py`` enforces it.  Before
this module existed, 22 files spawned git their own way in five spellings:
19 re-typed an env dict (only 6 neutering global/system config) and 3 passed
no env at all; a developer's ``commit.gpgsign`` or an ``/etc/gitconfig``
``core.hooksPath`` failed the un-neutered ones with an opaque
``CalledProcessError``.  Hermeticity is one decision, made once, here.

``dates=None`` (the default) deliberately does NOT pin commit timestamps:
20 of the 22 pre-existing files created "now" commits, and age-sensitive
consumers (coverage validity aging) must keep computing against real clocks
unless a test opts in.  Pass an ISO-8601 string to pin both author and
committer dates — that makes commit SHAs fully reproducible (identity and
message being equal); the changelog test and ``RepoTimeline`` do.
"""

import subprocess
from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git_env(home: Path | str, *, dates: str | None = None) -> dict[str, str]:
    """A closed environment for spawning git in tests.

    Identity pinned; global and system config neutered; ``HOME`` confined to
    *home* (nothing of the developer's dotfiles is reachable); prompts
    disabled (a git that decides to ask for credentials must fail, not
    block); ``PATH`` reduced to the system binaries — this assumes
    ``/usr/bin/git`` (true on the dev VM and ubuntu-latest CI; a Homebrew
    git in /usr/local/bin or /opt would need this widened).  The dict is
    COMPLETE — pass it as ``env=`` verbatim, never merged over
    ``os.environ``.
    """
    env = {
        **_IDENTITY,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    if dates is not None:
        env["GIT_AUTHOR_DATE"] = dates
        env["GIT_COMMITTER_DATE"] = dates
    return env


class TmpGitRepo:
    """A scratch git repository under *root* (created, ``init``-ed, hermetic).

    The three verbs cover what the 22 migrated files actually did: run a git
    command, write a file, commit everything.  Anything fancier belongs in
    the test, via ``self.git(...)``.
    """

    def __init__(self, root: Path, *, branch: str = "main", dates: str | None = None) -> None:
        self.root = root
        self.dates = dates
        self.root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", branch)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=git_env(self.root, dates=self.dates),
        ).stdout

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def commit(self, msg: str = "c", *, allow_empty: bool = False) -> str:
        """``add -A`` then commit, returning the new HEAD sha.

        Two traps, both deliberate: ``add -A`` sweeps EVERYTHING under the
        repo root into history — a test that writes artifacts into the repo
        between commits (an ``.otto/`` capture store, a planted
        ``.gitconfig``) must commit via ``self.git("commit", "-aqm", ...)``
        instead; and an empty commit is refused unless *allow_empty* says
        otherwise, so a test that forgot to ``write()`` fails here rather
        than pinning a vacuous history.
        """
        self.git("add", "-A")
        args = ["commit", "-qm", msg]
        if allow_empty:
            args.append("--allow-empty")
        self.git(*args)
        return self.git("rev-parse", "HEAD").strip()
