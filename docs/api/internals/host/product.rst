product internals
=================

The names ``otto.host.product`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/host/product`.

.. currentmodule:: otto.host.product

.. autodata:: otto.host.product.LOGIN_HOME

.. autodata:: otto.host.product.LOGIN_HOME_PLACEHOLDER

.. autoclass:: otto.host.product.StageDirPlan

.. autofunction:: otto.host.product.validate_stage_dir

.. autofunction:: otto.host.product.stage_dir_key

.. autofunction:: otto.host.product.resolve_stage_dir

.. autofunction:: otto.host.product.cov_dir_of_name

.. autofunction:: otto.host.product.cov_dir_of

.. autofunction:: otto.host.product.stamp_cov_dir

.. autofunction:: otto.host.product.gcda_find_cmd

.. autofunction:: otto.host.product.gcda_delete_cmd

.. autofunction:: otto.host.product.sudo_gcda_delete

.. autodata:: otto.host.product.INSTRUMENTATION_MARKERS

.. autodata:: otto.host.product.ARCHIVE_SUFFIXES

.. autofunction:: otto.host.product.scan_for_instrumentation

.. autodata:: otto.host.product.PRODUCT_KINDS

.. autofunction:: otto.host.product.apply_declared_products

.. autofunction:: otto.host.product.defined_twice

.. autofunction:: otto.host.product.apply_product_providers
