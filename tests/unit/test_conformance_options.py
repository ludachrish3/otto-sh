"""``exec-options``' comparison on hand-built outcomes: a refusal, by name and by what it typed."""

from otto.host.errors import UnsupportedOptionError
from tests.conformance._options import OptionArm, Outcome, arm_mismatch

_ARM = OptionArm(
    "sudo+user",
    {"sudo": True, "user": "otto-cf-nobody"},
    "printf '%s\\n' otto-cf-token",
    UnsupportedOptionError,
    refused_options=["sudo", "user"],
    token="otto-cf-token",
)


def _refused(option: str, *, recorded: "list[str] | None" = None) -> Outcome:
    return Outcome(
        value=None,
        raised=UnsupportedOptionError(option=option, host="h", reason="r"),
        logged=[],
        typed_nothing=True,
        recorded=recorded or [],
    )


def test_a_refusal_naming_one_of_the_arm_s_refused_options_is_accepted():
    assert arm_mismatch(_ARM, _refused("user")) is None
    assert arm_mismatch(_ARM, _refused("sudo")) is None


def test_a_refusal_naming_another_option_is_caught():
    why = arm_mismatch(_ARM, _refused("expects"))
    assert why is not None
    assert "refused expects=" in why


def test_a_refusal_that_typed_its_command_is_caught_though_the_next_command_agrees():
    """A refusal that first ran the command: the next answer agrees, a record does not."""
    typed = ["otto.host DEBUG: 'framed write cmd=otto-cf-token'"]
    why = arm_mismatch(_ARM, _refused("sudo", recorded=typed))
    assert why is not None
    assert "the command was typed" in why
