"""Every OttoModel builds lazily, and every one of them still builds (spec 2026-09-26 §5.3)."""

import importlib
import pkgutil

import otto
from otto.models.base import OttoModel
from otto.models.monitor import EventCreateBody, EventUpdateBody


def _all_subclasses(cls):
    """Recurse ``__subclasses__()``, yielding only otto's own classes.

    ``__subclasses__()`` reflects every class currently loaded in the
    process, including test-local doubles defined elsewhere in the suite —
    under pytest-xdist those may or may not be loaded yet depending on which
    other test files this worker already ran. Filtering to otto's own
    modules keeps this test's result independent of that order. Recursion
    still visits every subclass regardless of its module, in case a genuine
    otto class happens to subclass a test double.
    """
    for sub in cls.__subclasses__():
        if sub.__module__.startswith("otto."):
            yield sub
        yield from _all_subclasses(sub)


def _import_all_otto_modules():
    for info in pkgutil.walk_packages(otto.__path__, "otto."):
        if ".tests" in info.name or info.name.startswith("otto._webassets"):
            continue
        # otto.__main__ runs `main()` at module scope (the ``python -m otto``
        # entry point) so the real console script and `python -m otto` can
        # never diverge. Importing it here would dispatch the CLI against
        # pytest's own argv and exit/raise; it carries no OttoModel.
        if info.name == "otto.__main__":
            continue
        importlib.import_module(info.name)


# Sanctioned exceptions: request-body models FastAPI wraps in its own
# `TypeAdapter(Annotated[<model>, Field(alias=<param name>)])`, built inside a
# `warnings.catch_warnings()` block that only covers an EAGER build. A
# deferred model's real build happens later, on the request path, outside
# that suppression window, turning a harmless pydantic warning into a hard
# error under this repo's warnings-as-errors test config. See each class's
# own docstring in ``otto.models.monitor`` for the full mechanism. Matched by
# identity (not name): a rename would otherwise silently drop the class out
# of this set while leaving the string behind, exempting nothing.
_EAGER_BUILD_EXCEPTIONS = frozenset({EventCreateBody, EventUpdateBody})


def test_every_otto_model_defers_its_build():
    _import_all_otto_modules()
    subclasses = list(_all_subclasses(OttoModel))
    assert subclasses
    found_exceptions = set()
    for cls in subclasses:
        if cls in _EAGER_BUILD_EXCEPTIONS:
            found_exceptions.add(cls)
            assert cls.model_config.get("defer_build") is False, cls.__qualname__
        else:
            assert cls.model_config.get("defer_build") is True, cls.__qualname__
    # A stale exemption (the class renamed or removed) must fail loudly
    # rather than silently exempt nothing.
    assert found_exceptions == _EAGER_BUILD_EXCEPTIONS


def test_every_otto_model_builds():
    _import_all_otto_modules()
    for cls in _all_subclasses(OttoModel):
        cls.model_rebuild(force=True)
        assert cls.__pydantic_complete__, cls.__qualname__
