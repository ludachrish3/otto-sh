"""Unit tests for ``otto.coverage.collect`` — the composed collection workflow.

These exercise the single canonical fetch/metadata/capture implementation
(:func:`otto.coverage.collect.collect_coverage`) and the pre-run cleanup
(:func:`otto.coverage.collect.clean_remote_gcda`). Moved here (library-extraction
Task 15) from ``tests/unit/cli/test_test.py``'s coverage-collection suites and
adapted to the new fail-loud contract: ``collect_coverage`` never swallows —
the "collected nothing", ambiguous-tier, and non-git cases now *raise*, and the
never-fail-a-successful-run swallow policy lives one layer up in
:func:`otto.suite.run._post_run_coverage` (see ``TestPostRunSwallowPolicy``).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.coverage.collect import CollectResult, collect_coverage


@pytest.fixture
def sut_repo(tmp_path):
    """A real tmp_path git repo standing in for the SUT checkout."""
    import subprocess

    root = tmp_path / "sut"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@x",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@x",
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
            },
        )

    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


# ── No [coverage] config → fail loud ─────────────────────────────────────────


class TestNoCoverageConfig:
    """A tree with no ``[coverage]`` section fails loud (ValueError)."""

    def test_no_coverage_config_raises_valueerror(self, tmp_path):
        repo = MagicMock()
        repo.settings = {}  # no [coverage] section
        with pytest.raises(ValueError, match=r"\[coverage\]"):
            asyncio.run(collect_coverage(tmp_path, repos=[repo]))


# ── Fetch destination + fail-loud "collected nothing" ────────────────────────


class TestFetchStage:
    """The Unix fetch constructs its GcdaFetcher at the given ``cov_dir``, and a
    run that retrieves no ``.gcda`` from any host fails loud naming the hosts
    searched (``_do_get``'s message shape)."""

    def test_fetcher_uses_given_cov_dir_and_empty_fails_loud(self, tmp_path):
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        repo = MagicMock()
        repo.sut_dir = tmp_path
        repo.name = "repo"

        host = MagicMock(spec=UnixHost)
        host.id = "carrot"

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"gcda_remote_dir": "/remote"},
            ),
            patch("otto.config.all_hosts", return_value=[host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as fetcher_cls,
            pytest.raises(ValueError, match=r"no \.gcda counters retrieved from any host"),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        # The fetcher stages into the exact cov_dir it was handed (no override
        # logic — the caller resolves the destination now).
        fetcher_cls.assert_called_once_with(cov_dir)

    def test_empty_message_names_hosts_searched(self, tmp_path):
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        repo = MagicMock()
        repo.sut_dir = tmp_path
        repo.name = "repo"

        h1 = MagicMock(spec=UnixHost)
        h1.id = "carrot"
        h2 = MagicMock(spec=UnixHost)
        h2.id = "tomato"

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"gcda_remote_dir": "/remote"},
            ),
            patch("otto.config.all_hosts", return_value=[h1, h2]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
            pytest.raises(ValueError, match=r"searched: carrot, tomato"),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))


# ── clean_after_fetch — post-fetch remote-clean toggle ───────────────────────


class TestCleanAfterFetch:
    """``clean_after_fetch`` gates ``collect_coverage``'s *internal* post-fetch
    remote clean. Default ``True`` preserves ``otto test --cov`` semantics (zero
    the remotes right after a successful Unix fetch); ``False`` skips that clean
    so a caller (``otto cov get``) can own the post-fetch clean itself, scoped to
    its own host selection. Either way the fetch itself is unchanged."""

    def _run(self, cov_dir, *, clean_after_fetch=True):
        from otto.host import UnixHost

        repo = MagicMock()
        repo.sut_dir = cov_dir.parent
        repo.name = "repo"

        host = MagicMock(spec=UnixHost)
        host.id = "carrot"
        board = cov_dir / "carrot"

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={"carrot": board})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"gcda_remote_dir": "/remote"},
            ),
            patch("otto.config.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            # get_cov_repo None short-circuits the metadata + capture tail so the
            # test pins only the fetch/clean seam.
            patch("otto.coverage.config.get_cov_repo", return_value=None),
        ):
            result = asyncio.run(
                collect_coverage(cov_dir, repos=[repo], clean_after_fetch=clean_after_fetch)
            )
        return result, fetcher_instance, board

    def test_true_default_calls_clean_remote_when_unix_dirs_nonempty(self, tmp_path):
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        result, fetcher_instance, board = self._run(cov_dir)  # default True
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")
        assert result.host_dirs == {"carrot": board}

    def test_false_skips_internal_clean_remote(self, tmp_path):
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        result, fetcher_instance, board = self._run(cov_dir, clean_after_fetch=False)
        fetcher_instance.clean_remote.assert_not_awaited()
        # The fetch still happened and its dirs are reported unchanged.
        assert result.host_dirs == {"carrot": board}


# ── Embedded collection + metadata sidecar (moved from TestRunCoverageEmbedded)


class TestCollectEmbedded:
    """``collect_coverage`` collects embedded hosts even with no Unix
    ``gcda_remote_dir``, and its ``.otto_cov_meta.json`` sidecar behaves exactly
    as the old coverage-metadata writer did. Where a ``[coverage]`` repo resolves
    (so the capture tail would fire), ``produce_captures`` is stubbed — these
    tests pin metadata, not capture production."""

    def test_collects_embedded_when_only_embedded_configured(self, tmp_path):
        repo = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        embedded_collect = AsyncMock(return_value={"sprout": cov_dir / "sprout"})
        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"embedded": {"extension": "cov_ext"}},
            ),
            patch("otto.config.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=None),
        ):
            result = asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        embedded_collect.assert_awaited_once()
        assert isinstance(result, CollectResult)
        assert result.host_dirs == {"sprout": cov_dir / "sprout"}
        # No [coverage] repo → no captures produced.
        assert result.captures_written == []

    def test_unix_hop_host_not_treated_as_coverage_target(self, tmp_path):
        """A Unix SSH hop in the lab must not pollute the embedded meta.

        An embedded coverage lab must include the SSH hop (e.g. ``basil``
        fronting ``sprout_cov``) so the hop resolves — but the hop is
        infrastructure, not a coverage target, and emits no ``.gcda``. The meta
        must therefore (a) keep ``sut_dir`` = the embedded build dir (the hop must
        not flip it to the repo dir, which breaks ``.gcno`` discovery and made
        ``geninfo`` skip the file on the real lab) and (b) carry only the embedded
        host's toolchain, not the hop's. Regression for the basil-hop report bug.
        """
        from otto.host import UnixHost
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build" / "cov_ext_app"
        build_dir.mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"  # NOT what sut_dir should resolve to

        hop = MagicMock(spec=UnixHost)
        hop.id = "basil_seed"  # a Unix hop, produces no coverage

        sprout_cov = ZephyrHost(
            ip="192.0.2.33",
            element="sprout_cov",
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(return_value={"sprout-cov": cov_dir / "sprout-cov"})
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[hop, sprout_cov]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
            patch("otto.coverage.capture.produce.produce_captures", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["sut_dir"] == str(build_dir.resolve())
        # element "sprout_cov" slugs to the id "sprout-cov" (underscore -> hyphen).
        assert set(meta["toolchains"]) == {"sprout-cov"}
        assert "basil_seed" not in meta["toolchains"]

    def test_embedded_toolchain_is_per_host(self, tmp_path):
        """Each embedded host's coverage toolchain comes from host.toolchain."""
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(
            ip="192.0.2.33",
            element="sprout_cov",
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        (build_dir / "zephyr").mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        embedded_collect = AsyncMock(return_value={"sprout-cov": cov_dir / "sprout-cov"})
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
            patch("otto.coverage.capture.produce.produce_captures", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        # element "sprout_cov" slugs to the id "sprout-cov" (underscore -> hyphen).
        entry = meta["toolchains"]["sprout-cov"]
        assert entry["gcov"] == "bin/arm-zephyr-eabi-gcov"
        assert entry["sysroot"] == "/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"
        assert entry["lcov"] == "/usr/bin/lcov"

    def test_embedded_toolchain_falls_back_to_gcno_discovery(self, tmp_path):
        """A host left at the default Toolchain() resolves via .gcno discovery."""
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(ip="192.0.2.33", element="sprout_cov", transfer="console")
        # No toolchain configured -> default Toolchain() -> discovery fallback.
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        embedded_collect = AsyncMock(return_value={"sprout-cov": cov_dir / "sprout-cov"})
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }

        discovered = Toolchain(
            sysroot=Path("/discovered"),
            gcov=Path("bin/x-gcov"),
            lcov=Path("/usr/bin/lcov"),
        )

        def _fake_discover(build_dir_arg):
            return discovered

        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
            patch("otto.host.toolchain_discovery.discover_toolchain_from_gcno", new=_fake_discover),
            patch("otto.coverage.capture.produce.produce_captures", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        # element "sprout_cov" slugs to the id "sprout-cov"; the collect result is
        # keyed by that id, so the host resolves and its toolchain is recorded.
        assert meta["toolchains"]["sprout-cov"]["gcov"] == "bin/x-gcov"
        assert meta["toolchains"]["sprout-cov"]["sysroot"] == "/discovered"

    def test_coverage_hosts_regex_passed_to_both_selectors(self, tmp_path):
        """``[coverage].hosts`` compiles to a regex handed to the Unix and
        embedded host selectors, so the collect-from set is repo-declared
        rather than inferred from which hosts happened to emit ``.gcda``.
        """
        repo = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"hosts": "sprout_cov", "embedded": {"extension": "cov_ext"}},
            ),
            patch("otto.config.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=None),
            # No host produced .gcda → fail loud (the selectors still ran).
            pytest.raises(ValueError, match=r"no \.gcda counters"),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        unix_pat = all_hosts_mock.call_args.kwargs.get("pattern")
        assert unix_pat is not None
        assert unix_pat.search("sprout_cov")
        assert not unix_pat.search("basil_seed")

        emb_pat = embedded_collect.await_args.kwargs.get("pattern")
        assert emb_pat is not None
        assert emb_pat.pattern == "sprout_cov"

    def test_unset_coverage_hosts_passes_no_pattern(self, tmp_path):
        """Unset ``[coverage].hosts`` → ``pattern=None`` (collect from all hosts)."""
        repo = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"embedded": {"extension": "cov_ext"}},
            ),
            patch("otto.config.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=None),
            pytest.raises(ValueError, match=r"no \.gcda counters"),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        assert all_hosts_mock.call_args.kwargs.get("pattern") is None
        assert embedded_collect.await_args.kwargs.get("pattern") is None

    def test_per_version_source_roots_recorded(self, tmp_path):
        """Two embedded hosts of different os_version each record their own build_dir
        as a per-host source root in the meta (multi-Zephyr-version coverage).
        """
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build37 = tmp_path / "build" / "v3_7"
        build37.mkdir(parents=True)
        build44 = tmp_path / "build" / "v4_4"
        build44.mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        sprout = ZephyrHost(
            ip="192.0.2.33",
            element="sprout",
            transfer="console",
            os_version="3.7",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk37/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        sprout44 = ZephyrHost(
            ip="192.0.2.34",
            element="sprout44",
            transfer="console",
            os_version="4.4",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk44/gnu/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(
            return_value={
                "sprout": cov_dir / "sprout",
                "sprout44": cov_dir / "sprout44",
            }
        )
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "builds": {
                    "3.7": {"build_dir": str(build37)},
                    "4.4": {"build_dir": str(build44)},
                },
            },
        }
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[sprout, sprout44]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
            patch("otto.coverage.capture.produce.produce_captures", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(collect_coverage(cov_dir, repos=[repo]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["source_roots"]["sprout"] == str(build37.resolve())
        assert meta["source_roots"]["sprout44"] == str(build44.resolve())


# ── Capture tail — fail loud (no swallowing inside collect_coverage) ──────────


class TestCaptureTail:
    """The post-collection capture.json tail runs against the resolved tier and
    returns the produced paths in :class:`CollectResult`. Unlike the old inline
    tail it never swallows — an ambiguous tier or a non-git sut *raise*."""

    def _collect(self, repo, cov_dir, cov_config):
        """Drive collect_coverage with one embedded board already collected."""
        embedded_collect = AsyncMock(return_value={"board1": cov_dir / "board1"})
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
        ):
            return asyncio.run(collect_coverage(cov_dir, repos=[repo]))

    def test_happy_path_writes_pinned_capture(self, tmp_path, sut_repo, monkeypatch):
        """A well-formed single-tier repo leaves behind a real capture.json and
        returns it in CollectResult.captures_written."""
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{sut_repo / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"

        # Non-empty (truthy) but no explicit [coverage.tiers] -> implicit
        # single "system" e2e tier.
        result = self._collect(repo, cov_dir, {"tiers": {}})

        capture_path = cov_dir / "board1" / "capture.json"
        assert capture_path.is_file()
        assert json.loads(capture_path.read_text())["tier"] == "system"
        assert result.captures_written == [capture_path]

    def test_ambiguous_tiers_raise(self, tmp_path, sut_repo, monkeypatch):
        """Two e2e-kind tiers make ``resolve_get_tier`` raise ``ValueError`` —
        which now propagates out of collect_coverage (no swallow)."""
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{sut_repo / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"

        cov_config = {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "nightly": {"kind": "e2e", "precedence": 2},
            }
        }

        with pytest.raises(ValueError, match=r"e2e-kind tiers"):
            self._collect(repo, cov_dir, cov_config)

        assert not (cov_dir / "board1" / "capture.json").exists()

    def test_non_git_sut_raises(self, tmp_path, monkeypatch):
        """A non-git sut dir now raises (GitUnavailableError, a RuntimeError)."""
        from otto.coverage.capture import produce as produce_mod
        from otto.coverage.capture.gitio import GitUnavailableError

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        notgit = tmp_path / "notgit"
        notgit.mkdir()
        (notgit / "f.c").write_text("int a;\n")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{notgit / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = notgit
        repo.name = "repo"

        with pytest.raises(GitUnavailableError):
            self._collect(repo, cov_dir, {"tiers": {}})

    def test_annotations_passed_through_to_produce(self, tmp_path):
        """tier/ticket/note/tester/display_names thread through to produce_captures."""
        repo = MagicMock()
        repo.sut_dir = tmp_path / "sut"
        repo.name = "repo"
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        produce_mock = AsyncMock(return_value=[cov_dir / "board1" / "capture.json"])
        embedded_collect = AsyncMock(return_value={"board1": cov_dir / "board1"})
        cov_config = {
            "tiers": {
                "manual": {"kind": "manual", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
            }
        }
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            result = asyncio.run(
                collect_coverage(
                    cov_dir,
                    repos=[repo],
                    tier="manual",
                    ticket="JIRA-1",
                    note="hi",
                    tester={"name": "chris"},
                    display_names={"board1": "Board One"},
                )
            )

        kwargs = produce_mock.await_args.kwargs
        assert kwargs["tier"] == "manual"
        assert kwargs["ticket"] == "JIRA-1"
        assert kwargs["note"] == "hi"
        assert kwargs["tester"] == {"name": "chris"}
        assert kwargs["display_names"] == {"board1": "Board One"}
        assert result.captures_written == [cov_dir / "board1" / "capture.json"]


# ── Swallow policy relocated to _post_run_coverage ───────────────────────────


class TestPostRunSwallowPolicy:
    """The never-fail-a-successful-run policy now lives in
    :func:`otto.suite.run._post_run_coverage`: a coverage-collection failure is
    logged and swallowed, leaving raw artifacts on disk. (Moved from the old
    coverage-collection swallow tests.)"""

    def _drive_post_run(self, repo, cov_dir, cov_config):
        from otto.suite.run import RunOptions, _post_run_coverage

        embedded_collect = AsyncMock(return_value={"board1": cov_dir / "board1"})
        opts = RunOptions(cov=True, cov_report=False, cov_dir=cov_dir)
        with (
            patch("otto.coverage.config.get_cov_config", return_value=cov_config),
            patch("otto.config.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.coverage.config.get_cov_repo", return_value=repo),
        ):
            asyncio.run(_post_run_coverage([repo], cov_dir.parent / "log", opts))

    def test_ambiguous_tiers_do_not_fail_the_run(self, tmp_path, sut_repo, caplog):
        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"

        cov_config = {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "nightly": {"kind": "e2e", "precedence": 2},
            }
        }

        with caplog.at_level("WARNING"):
            self._drive_post_run(repo, cov_dir, cov_config)

        assert not (cov_dir / "board1" / "capture.json").exists()
        assert any("Coverage collection failed" in rec.message for rec in caplog.records)

    def test_non_git_sut_does_not_fail_the_run(self, tmp_path, monkeypatch, caplog):
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        notgit = tmp_path / "notgit"
        notgit.mkdir()
        (notgit / "f.c").write_text("int a;\n")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{notgit / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = notgit
        repo.name = "repo"

        with caplog.at_level("WARNING"):
            self._drive_post_run(repo, cov_dir, {"tiers": {}})

        assert not (cov_dir / "board1" / "capture.json").exists()
        assert any("Coverage collection failed" in rec.message for rec in caplog.records)


# ── clean_remote_gcda (pre-run cleanup + connection rebuild) ──────────────────


class TestCleanRemoteGcda:
    """``clean_remote_gcda`` zeroes remote counters (when configured) and always
    rebuilds Unix host connections so pytest reconnects on its own loop."""

    def test_cleans_and_rebuilds_when_configured(self):
        from otto.coverage.collect import clean_remote_gcda
        from otto.host import UnixHost

        host = MagicMock(spec=UnixHost)
        fetcher_instance = MagicMock()
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"gcda_remote_dir": "/remote"},
            ),
            patch("otto.config.all_hosts", return_value=[host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as fetcher_cls,
        ):
            asyncio.run(clean_remote_gcda([MagicMock()]))

        fetcher_cls.assert_called_once()
        fetcher_instance.clean_remote.assert_awaited_once_with("/remote")
        host.rebuild_connections.assert_called_once()

    def test_no_config_skips_clean_but_still_rebuilds(self):
        from otto.coverage.collect import clean_remote_gcda
        from otto.host import UnixHost

        host = MagicMock(spec=UnixHost)
        with (
            patch("otto.coverage.config.get_cov_config", return_value={}),
            patch("otto.config.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher") as fetcher_cls,
        ):
            asyncio.run(clean_remote_gcda([MagicMock()]))

        fetcher_cls.assert_not_called()
        host.rebuild_connections.assert_called_once()

    def test_no_gcda_remote_dir_warns_and_rebuilds(self, caplog):
        from otto.coverage.collect import clean_remote_gcda
        from otto.host import UnixHost

        host = MagicMock(spec=UnixHost)
        with (
            patch(
                "otto.coverage.config.get_cov_config",
                return_value={"embedded": {"extension": "cov_ext"}},
            ),
            patch("otto.config.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher") as fetcher_cls,
            caplog.at_level("WARNING"),
        ):
            asyncio.run(clean_remote_gcda([MagicMock()]))

        fetcher_cls.assert_not_called()
        host.rebuild_connections.assert_called_once()
        assert any("gcda_remote_dir not configured" in rec.message for rec in caplog.records)
