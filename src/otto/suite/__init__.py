"""Public API for otto test suites: ``OttoSuite``, ``register_suite``, and ``OttoOptionsPlugin``."""

from .register import (
    OttoOptionsPlugin as OttoOptionsPlugin,
)
from .register import (
    register_suite as register_suite,
)
from .suite import (
    OttoSuite as OttoSuite,
)
