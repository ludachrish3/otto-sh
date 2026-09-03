"""The ``[coverage].hosts`` selector: one loader, refused by name when malformed.

The selector comes straight out of ``settings.toml`` (a ``dict[str, Any]``);
a non-string value used to fall through to ``re.compile`` and die with a raw
``TypeError`` traceback, and a falsy value (``""``, ``[]``, ``false``) used to
silently mean "no selector" — fanning coverage (and ``otto cov clean``'s
deletes) out to every host including the SSH hop the selector exists to
exclude. :func:`load_hosts_pattern` refuses both by name, through the same
:class:`CoverageConfigError` contract every caller already prints cleanly.
"""

import re
from types import SimpleNamespace

import pytest

from otto.coverage.collect import collect_coverage
from otto.coverage.config import load_hosts_pattern
from otto.coverage.errors import CoverageConfigError


class TestLoadHostsPattern:
    def test_absent_key_means_no_selector(self):
        assert load_hosts_pattern({}) is None

    def test_a_string_compiles(self):
        pattern = load_hosts_pattern({"hosts": "web.*"})
        assert isinstance(pattern, re.Pattern)
        assert pattern.pattern == "web.*"

    @pytest.mark.parametrize("bad", [["host1", "host2"], 123, False], ids=["list", "int", "bool"])
    def test_non_string_is_refused_by_name(self, bad):
        with pytest.raises(CoverageConfigError, match="hosts must be a string"):
            load_hosts_pattern({"hosts": bad})

    def test_empty_string_is_refused_not_treated_as_no_selector(self):
        with pytest.raises(CoverageConfigError, match="hosts must not be empty"):
            load_hosts_pattern({"hosts": ""})


@pytest.mark.asyncio
async def test_collect_coverage_refuses_a_non_string_selector(tmp_path):
    # The caller-level contract: the loader's refusal propagates out of
    # collect_coverage as the CoverageConfigError its callers already print.
    repo = SimpleNamespace(settings={"coverage": {"hosts": ["host1", "host2"]}})
    with pytest.raises(CoverageConfigError, match="hosts must be a string"):
        await collect_coverage(tmp_path, repos=[repo])
