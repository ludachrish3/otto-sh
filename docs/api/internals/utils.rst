utils internals
===============

The names ``otto.utils`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/utils`.

.. currentmodule:: otto.utils

.. autofunction:: otto.utils.anchor_path

.. autofunction:: otto.utils.parse_cache_ttl

.. autodata:: otto.utils.MIN_INTERVAL_SECONDS

.. autofunction:: otto.utils.compile_host_pattern

.. autofunction:: otto.utils.split_on

.. autofunction:: otto.utils.complete_separated_list

.. autofunction:: otto.utils.complete_marker_expression

.. autoexception:: otto.utils.WaitTimeoutError

.. autofunction:: otto.utils.wait_for

.. autofunction:: otto.utils.wait_for_async

.. autodata:: otto.utils.DRY_RUN_HEADLINE

.. autodata:: otto.utils.DRY_RUN_HEADLINE_PROBED

.. autodata:: otto.utils.T

.. autofunction:: otto.utils.is_literal
