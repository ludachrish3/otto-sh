# Fixture wheels

## `otto-fixture-beetroot`

A one-function package that exists only so a test can name a Python dependency
that **cannot** be satisfied from any index. If a test ever installs it
successfully without pointing at this directory, something has reached the
network that should not have.

**The name is load-bearing, and the first attempt got it wrong.** This started
out as a bare `beetroot`, which is a *real* PyPI project (1.1.7.4): `uv pip
install -e tests/repo4` fetched it happily, and every test resting on "this
cannot resolve" would have passed for the wrong reason. `otto-fixture-beetroot`
was checked against the live index and is absent. If you ever rename it, check
the replacement the same way.

`tests/repo4` declares it in its `pyproject.toml`, which makes repo4 the sample
repo whose environment is *incomplete until installed* — the shape the
`otto env` and preflight suites need.

The wheel is **committed**, not built at test time. A test run therefore needs
no build backend, no `setuptools`, and no network, and the artifact under test
is byte-identical on every machine and every CI leg.

`build_fixture_beetroot.py` is the wheel's source of truth: it writes a PEP-427 zip
(package + `METADATA`, `WHEEL`, `RECORD`) with the standard library alone.
`tests/unit/env/test_fixture_wheel.py` re-runs it and compares member content
against the committed archive, so a hand-edited wheel fails rather than drifts.

Regenerate after editing the builder:

```bash
python tests/_fixtures/wheels/build_fixture_beetroot.py
```

**If you change the wheel's contents, bump its version** (or clear your
installer caches). uv and pip key their wheel caches on name + version, so a
rebuilt `0.1.0` with different bytes is served from cache as the OLD one — the
tests then exercise a wheel that no longer exists on disk, and the failure
reads like a broken assertion rather than a stale cache. `uv cache clean
otto-fixture-beetroot` is the escape.
