params internals
================

The names ``otto.params`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/params`.

.. currentmodule:: otto.params

.. autofunction:: otto.params.build_options

.. autofunction:: otto.params.contains_secret_type

.. autofunction:: otto.params.sensitive_field_names

.. autofunction:: otto.params.options_params

.. autofunction:: otto.params.declaring_class

.. autofunction:: otto.params.shared_field_values

.. autoclass:: otto.params.OptionsSource

.. autoclass:: otto.params.OptionsEntry

.. autoclass:: otto.params.OptionsOrigin

.. autofunction:: otto.params.verb_option_classes

.. autofunction:: otto.params.authored_flags

.. autofunction:: otto.params.flag_spellings

.. autofunction:: otto.params.merge_option_params

.. autofunction:: otto.params.flatten_option_instances

.. autoclass:: otto.params.RecordedOptions
