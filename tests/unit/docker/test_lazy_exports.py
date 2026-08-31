"""otto.docker's adapter and deploy re-exports are lazy (PEP 562), not eager.

Only a caller that actually names ``AdapterResult``/``register_compose_adapter``
(``.adapter``) or ``deploy``/``teardown``/``deployed``/``UseCaseStack``
(``.deployment``) should pay for importing those modules — every other importer of
``otto.docker`` (the CLI surface among them) must not.
See ``tests/unit/import_budget/`` for the surface-level snapshot guard; this
test proves the runtime attribute-resolution path directly, the same way
``tests/unit/link/test_lazy_exports.py`` proves it for ``otto.link``'s
``.manage`` re-exports.
"""

import subprocess
import sys
from types import ModuleType

import pytest

from otto.docker import adapter, deployment


def test_lazy_names_all_resolve_to_their_source_module_objects():
    import otto.docker as docker_mod

    for name in ("AdapterResult", "register_compose_adapter"):
        assert getattr(docker_mod, name) is getattr(adapter, name)
    for name in ("UseCaseStack", "deploy", "deployed", "teardown"):
        assert getattr(docker_mod, name) is getattr(deployment, name)


def test_dir_includes_the_lazy_exports():
    import otto.docker as docker_mod

    assert {
        "AdapterResult",
        "register_compose_adapter",
        "UseCaseStack",
        "deploy",
        "deployed",
        "teardown",
    } <= set(dir(docker_mod))


def test_a_lazy_export_never_degrades_into_its_own_submodule():
    """The pin behind naming the module ``.deployment`` rather than ``.deploy``.

    A submodule and a lazy export sharing a name is won by the SUBMODULE:
    the first ``otto.docker.deploy`` access imports it and rebinds the
    package attribute, so the export would resolve to the function once and
    to the module forever after -- silently. Asserted twice in a row,
    because once is exactly what used to work, and driven off ``_LAZY_ATTRS``
    so a lazy name added later cannot arrive unguarded.
    """
    import otto.docker as docker_mod

    # _LAZY_ATTRS, not a literal tuple: the guard is about a FUTURE submodule
    # colliding with an export, so it has to cover whatever is lazy today
    # rather than whatever was lazy when the test was written (review M9).
    assert set(docker_mod._LAZY_ATTRS) == {
        "AdapterResult",
        "register_compose_adapter",
        "UseCaseStack",
        "deploy",
        "deployed",
        "teardown",
    }
    for name in docker_mod._LAZY_ATTRS:
        first = getattr(docker_mod, name)
        assert getattr(docker_mod, name) is first, (
            f"otto.docker.{name} resolved to two different objects in one process."
        )
        assert not isinstance(first, ModuleType), (
            f"otto.docker.{name} resolved to a MODULE — a submodule of that name is "
            f"shadowing the export; rename the module."
        )


def test_unknown_attribute_raises_attribute_error():
    import otto.docker as docker_mod

    with pytest.raises(AttributeError, match=r"module 'otto\.docker' has no attribute 'nope'"):
        _ = docker_mod.nope


@pytest.mark.parametrize(
    ("module", "attr"),
    [("otto.docker.adapter", "AdapterResult"), ("otto.docker.deployment", "deploy")],
)
def test_bare_import_does_not_pull_the_lazy_module(module, attr):
    """Fresh subprocess: importing otto.docker alone must not import the lazy
    module until one of its names is actually accessed."""
    code = f"import sys; import otto.docker; print({module!r} in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout

    code_after_access = (
        f"import sys; import otto.docker; otto.docker.{attr}; print({module!r} in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code_after_access],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True", out.stdout
