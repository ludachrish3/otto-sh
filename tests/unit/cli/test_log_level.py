"""``--log-level``'s completer: the level names, in the case the fragment is typed in."""

from types import SimpleNamespace

from otto.cli.main import _log_level_completer
from otto.logger.levels import LEVEL_NAMES


def _complete(fragment: str) -> list[str]:
    return _log_level_completer(SimpleNamespace(), fragment)  # ty: ignore[invalid-argument-type]


def test_completer_answers_in_the_fragments_case():
    assert _complete("deb") == ["debug"]
    assert _complete("DEB") == ["DEBUG"]
    assert _complete("") == LEVEL_NAMES
    assert _complete("w") == ["warning", "warn"]


def test_completer_drops_a_mixed_case_fragment():
    assert _complete("De") == []
