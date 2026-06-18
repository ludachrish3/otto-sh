"""Unit tests for the BinaryLoader strategy and its registry."""

import pytest

from otto.host.binary_loader import (
    BinaryLoader,
    LlextHexLoader,
    build_binary_loader,
    register_binary_loader,
)


class TestLlextHexLoader:
    loader = LlextHexLoader()

    def test_type_name(self):
        assert LlextHexLoader.type_name == "llext-hex"

    def test_load_command_hex_encodes_payload(self):
        assert self.loader.load_command("cov_ext", b"\x01\xab\xff") == "llext load_hex cov_ext 01abff"

    def test_check_loaded_true_on_success_marker(self):
        ok, reason = self.loader.check_loaded("uart:~$ Successfully loaded extension cov_ext")
        assert ok is True
        assert reason == ""

    def test_check_loaded_false_returns_output_as_reason(self):
        ok, reason = self.loader.check_loaded("Failed to load: return code -8")
        assert ok is False
        assert "Failed to load" in reason

    def test_unload_command(self):
        assert self.loader.unload_command("cov_ext") == "llext unload cov_ext"

    def test_is_fully_unloaded_only_on_no_such_extension(self):
        assert self.loader.is_fully_unloaded("No such extension cov_ext") is True
        assert self.loader.is_fully_unloaded("Unloaded extension cov_ext") is False

    def test_max_unload_rounds_default(self):
        assert LlextHexLoader.max_unload_rounds == 16


class TestRegistry:
    def test_builtin_resolves_by_name(self):
        assert isinstance(build_binary_loader("llext-hex"), LlextHexLoader)

    def test_is_a_binary_loader(self):
        assert isinstance(build_binary_loader("llext-hex"), BinaryLoader)

    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown binary loader"):
            build_binary_loader("does-not-exist")

    def test_register_then_build(self):
        class CustomLoader(LlextHexLoader):
            type_name = "custom-loader-test"

        register_binary_loader("custom-loader-test", CustomLoader)
        assert isinstance(build_binary_loader("custom-loader-test"), CustomLoader)

    def test_register_rejects_name_mismatch(self):
        class Mismatch(LlextHexLoader):
            type_name = "right-name"

        with pytest.raises(ValueError, match="doesn't match"):
            register_binary_loader("wrong-name", Mismatch)


def test_builtins_registered_via_public_path():
    from otto.host import binary_loader as bl

    assert len(bl._LOADER_CLASSES) >= 1  # at least the built-in loader(s)
