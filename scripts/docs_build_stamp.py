"""Decide whether a docs build sits exactly on a release tag.

otto's version is STATIC in pyproject.toml (bump-my-version keeps it equal to the
latest tag), so `importlib.metadata.version("otto-sh")` reads "0.9.0" both at the
tag and three commits past it. The version string alone therefore cannot tell a
reader whether the docs they are on are released or work-in-progress — that
signal has to come from the build environment.

Read the Docs' default version is `latest`, which tracks the branch, so the bare
https://otto-sh.readthedocs.io/ redirects readers to development docs without
their ever passing a version selector. Hence the banner.

Three tiers, first hit wins:

1. Read the Docs environment variables. Authoritative on RTD, and unlike git it
   does not depend on the shallow clone happening to carry tags.
2. `git describe` in the checkout. Covers local `make docs` and CI.
3. Neither available (an sdist build with no .git). Assume RELEASE and stay
   quiet. The failure modes are asymmetric: a missing banner on dev docs is a
   small loss, but a "development documentation" banner on the released docs
   actively misinforms, so silence is the safe default.

Consumed by docs/conf.py. Unit-tested in tests/unit/test_docs_build_stamp.py.
"""

import dataclasses
import os
import pathlib
import subprocess
import typing

# Read the Docs resolves this alias server-side to the highest tag by SEMANTIC
# VERSION ordering among active, built, non-hidden tag versions — not the most
# recently created tag. That is exactly the "newest release" meaning we want:
# back-porting a v0.8.9 hotfix tomorrow would not displace v0.9.0 as stable.
# Deliberately not synthesised from the stamp's base_tag: a URL we build by hand
# 404s the moment that tag's RTD version is deactivated or hidden, and would be
# wrong outright whenever the git tier is unavailable.
STABLE_DOCS_URL = "https://otto-sh.readthedocs.io/en/stable/"

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# `git describe --long` always yields exactly "<tag>-<distance>-g<sha>".
_DESCRIBE_FIELDS = 3


@dataclasses.dataclass(frozen=True)
class BuildStamp:
    """What the build environment knows about this checkout's release status."""

    is_release: bool
    base_tag: str | None
    commit: str | None
    commits_since: int | None


def _from_rtd_env(env: typing.Mapping[str, str]) -> BuildStamp | None:
    version_type = env.get("READTHEDOCS_VERSION_TYPE")
    if not version_type:
        return None
    # "tag" is the only type that means released. "branch" is `latest`, and
    # "external" is a pull-request preview — neither may claim to be a release.
    is_release = version_type == "tag"
    commit = env.get("READTHEDOCS_GIT_COMMIT_HASH") or None
    return BuildStamp(
        is_release=is_release,
        # On a tag build READTHEDOCS_VERSION *is* the tag name. On a branch build
        # it is the branch slug ("latest"), which is not a tag, so report none
        # rather than pass a slug off as one.
        base_tag=env.get("READTHEDOCS_VERSION") if is_release else None,
        commit=commit[:8] if commit else None,
        commits_since=None,
    )


def _from_git(repo: pathlib.Path) -> BuildStamp | None:
    try:
        completed = subprocess.run(
            ["git", "describe", "--tags", "--long", "--dirty"],  # noqa: S607 — PATH by design
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # No git binary, or `repo` does not exist.
        return None
    if completed.returncode != 0:
        # Not a repository, or no tags are reachable from HEAD.
        return None

    described = completed.stdout.strip()
    dirty = described.endswith("-dirty")
    if dirty:
        described = described[: -len("-dirty")]
    # `--long` guarantees the "<tag>-<distance>-g<sha>" suffix, so splitting from
    # the RIGHT is safe even for tag names that themselves contain dashes.
    parts = described.rsplit("-", 2)
    if len(parts) != _DESCRIBE_FIELDS:
        return None
    base_tag, distance, sha = parts
    try:
        commits_since = int(distance)
    except ValueError:
        return None

    return BuildStamp(
        # A dirty tree does not match the tag it sits on, so it is not a release.
        is_release=commits_since == 0 and not dirty,
        base_tag=base_tag,
        commit=sha.lstrip("g"),
        commits_since=commits_since,
    )


def build_stamp(
    env: typing.Mapping[str, str] | None = None,
    repo: pathlib.Path | None = None,
) -> BuildStamp:
    """Classify this docs build. See the module docstring for the tier order."""
    env = os.environ if env is None else env
    repo = PROJECT_ROOT if repo is None else repo
    return (
        _from_rtd_env(env)
        or _from_git(repo)
        # Tier 3: no signal at all — stay quiet.
        or BuildStamp(is_release=True, base_tag=None, commit=None, commits_since=None)
    )


def html_title(project: str, release: str, stamp: BuildStamp) -> str:
    """Return the Sphinx page/tab title.

    The version is the bare release ("0.9.0"), never the git tag name — no
    leading "v" — and never the full describe string, which is too long for a
    browser tab. Untagged builds get a short "+dev" marker; the detail lives in
    the announcement banner instead.
    """
    marker = "" if stamp.is_release else "+dev"
    return f"{project} {release}{marker} documentation"


def dev_banner_text(stamp: BuildStamp) -> str | None:
    """Return the announcement-bar sentence, or None on a released build.

    Degrades gracefully: an RTD branch build on a shallow clone with no tags
    still gets a usable warning, just without the commit/distance detail.
    """
    if stamp.is_release:
        return None
    text = "This is the development documentation for otto"
    if stamp.commit:
        text += f", built from commit {stamp.commit}"
    if stamp.base_tag and stamp.commits_since:
        plural = "commit" if stamp.commits_since == 1 else "commits"
        text += f" — {stamp.commits_since} {plural} after {stamp.base_tag}"
    return text + "."
