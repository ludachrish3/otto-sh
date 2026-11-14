"""The A2 driver's dry run: every row on local shells with a fake sudo. No lab host is reached."""

import shutil
from pathlib import Path

import pytest

from tests.measurements import a2_core as core
from tests.measurements import a2_exec_frame as driver
from tests.measurements import a2_steps as steps

CASES = [
    pytest.param(shell, keeps, id=f"{shell}-{'timestamp' if keeps else 'no-timestamp'}")
    for shell in driver.DRY_SHELLS
    for keeps in (True, False)
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("shell", "keeps"), CASES)
async def test_the_dry_run_measures_every_row_and_leaves_the_shell_as_found(
    shell: str, keeps: bool, tmp_path: Path
) -> None:
    if shutil.which(driver.DRY_SHELLS[shell][0]) is None:
        pytest.skip(f"{shell} is not installed")
    run = await driver.dry_run_one(
        shell, tmp_path / "root", tmp_path / "out", keeps_timestamp=keeps
    )

    assert run.cell.uncertain is None
    assert run.end is not None
    assert core.differences(run.start, run.end) == []

    assert run.policy is not None
    assert run.policy.timestamp_timeout == ("15" if keeps else "0")
    assert run.policy.all_commands is True

    rows = run.sudo.rows
    # The C5b rule: it fires only where a validation is not kept for the
    # `sudo -n` right after it.
    assert run.sudo.needs_fallback is (not keeps)
    if keeps:
        assert rows["4.1 discriminator"] == "validated=0 status=0 asked_again=False"
        assert "4.8 wrapped" not in rows
    else:
        assert rows["4.1 discriminator"].startswith("validated=0 status=1 asked_again=True")
        assert rows["4.8 wrapped"] == "status=0 users=['root', 'root']"
    # One wrong line on a pipe: sudo asks, refuses, asks again and reads end of file.
    assert rows["4.6 wrong password"] == (
        "validated=1 status=1 asked_again=False prompts=2 command_ran=False"
    )
    assert rows["4.7 sudo -n true"] == "status=1"
    assert set(rows) >= {
        "4.2 second call",
        "4.3 id -un; id -un",
        "4.4 caller expects",
        "4.5 under set -e",
        "4.5 under set -eu",
        "4.5 caller expects under set -e",
        "4.5 caller expects under set -eu",
    }
    # The caller's answer reaches the command only where the command runs at all.
    assert f"got={keeps}" in rows["4.4 caller expects"]
    assert f"got={keeps}" in rows["4.5 caller expects under set -eu"]

    assert set(run.console) == {
        "i login shell",
        "ii clean login shell",
        "iii clean login shell with hook",
        "iv second login shell",
    }

    written = (tmp_path / "out").glob("*.txt")
    for path in written:
        text = path.read_text()
        assert "dry-run-password" not in text
        assert driver.WRONG_PASSWORD.get_secret_value() not in text


def test_the_lab_needs_both_guard_flags() -> None:
    for argv in (["--bed"], ["--i-hold-the-lease"]):
        with pytest.raises(SystemExit) as stopped:
            driver.main(argv)
        assert stopped.value.code == 2


def test_the_fallback_rule() -> None:
    def v(validated: "int | None", status: int, asked: bool) -> steps.Validation:
        return steps.Validation(validated, status, asked, "")

    assert steps.needs_fallback(v(0, 1, True)) is True
    assert steps.needs_fallback(v(0, 0, False)) is False  # the timestamp held
    assert steps.needs_fallback(v(1, 1, False)) is False  # the validation itself failed
    assert steps.needs_fallback(v(0, 3, False)) is False  # the command failed on its own
    assert steps.needs_fallback(v(None, 1, True)) is False  # nothing was validated


def test_policy_classes_are_read_from_sudo_s_listing() -> None:
    listing = (
        "Matching Defaults entries for test on host:\n"
        "    env_reset, timestamp_timeout=0, !requiretty, tty_tickets\n\n"
        "User test may run the following commands on host:\n    (ALL) NOPASSWD: ALL\n"
    )
    got = steps.policy_class("Sudo version 1.9.15p5", listing)
    assert got == steps.PolicyClass("1.9.15p5", "tty", "0", True, False, True)
    plain = steps.policy_class("Sudo version 1.8.31", "User x may run:\n    (root) /bin/ls\n")
    assert plain == steps.PolicyClass("1.8.31", "unset", "default", False, False, False)


def test_a_password_that_is_the_login_name_is_not_struck_from_the_records() -> None:
    same = driver.secrets_for("vagrant", driver.SecretStr("vagrant"))
    assert [s.get_secret_value() for s in same] == [driver.WRONG_PASSWORD.get_secret_value()]
    other = driver.secrets_for("test", driver.SecretStr("s3cret"))
    assert [s.get_secret_value() for s in other] == [
        "s3cret",
        driver.WRONG_PASSWORD.get_secret_value(),
    ]


@pytest.mark.asyncio
async def test_a_line_logged_in_as_someone_else_gets_no_sudo(tmp_path: Path) -> None:
    password = driver.SecretStr("dry-run-password")
    async with driver.dry_cell("dash", tmp_path, password=password, keeps_timestamp=True) as cell:
        policy = await steps.inventory(cell, password, login="somebody-else")
        assert policy is None
        assert cell.uncertain is not None
        assert "no sudo is typed" in cell.uncertain
        assert not any("sudo" in record.typed for record in cell.records)


@pytest.mark.asyncio
async def test_a_frame_that_never_ends_stops_the_cell_and_nothing_more_is_typed(
    tmp_path: Path,
) -> None:
    password = driver.SecretStr("dry-run-password")
    async with driver.dry_cell("dash", tmp_path, password=password, keeps_timestamp=True) as cell:

        async def waits_at_a_prompt() -> None:
            # `read` waits on the terminal, as an unanswered password prompt does.
            await steps.type_frame(cell, "read never", stdin_eof=False, timeout=1.0)

        await core.run_row(cell, "stuck", waits_at_a_prompt, deadline_s=30.0)
        typed_before = len(cell.records)
        assert cell.uncertain is not None
        await core.run_row(cell, "next", waits_at_a_prompt, deadline_s=30.0)
        assert [r.outcome for r in cell.records[typed_before:]] == [f"SKIPPED: {cell.uncertain}"]
