from pathlib import Path

import pytest

from otto.configmodule.lab import Lab


def test_lab_default() -> None:

    lab = Lab("lab1")

    assert lab.name == "lab1"
    assert lab.resources == set()
    assert lab.hosts == dict()
