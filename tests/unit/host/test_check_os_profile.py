"""``check_os_profile`` refuses what ``register_os_profile`` refuses, and registers nothing."""

import pytest

from otto.host import check_os_profile
from otto.host.os_profile import OS_PROFILES


def test_an_unknown_base_is_refused() -> None:
    with pytest.raises(ValueError, match="base must name a registered host class"):
        check_os_profile("acme-os", "windows")


def test_an_unknown_default_field_is_refused() -> None:
    with pytest.raises(
        ValueError, match=r"unknown default field\(s\) for base 'unix': \['osTyp'\]"
    ):
        check_os_profile("acme-os", "unix", {"osTyp": "unix"})


@pytest.mark.parametrize("field_name", ["login_prompt", "password_prompt"])
def test_an_invalid_prompt_regex_is_refused(field_name: str) -> None:
    with pytest.raises(ValueError, match=f"{field_name} is not a valid regex"):
        check_os_profile("acme-os", "unix", **{field_name: "("})


def test_a_valid_profile_registers_nothing() -> None:
    before = sorted(OS_PROFILES.names())
    check_os_profile("acme-os-unregistered", "unix", {}, login_prompt=r"login: ")
    assert sorted(OS_PROFILES.names()) == before
    assert "acme-os-unregistered" not in OS_PROFILES
