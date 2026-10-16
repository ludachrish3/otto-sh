config
======

The config package parses otto's inputs: environment variables and each
repository's ``settings.toml``. Lab data lives elsewhere: the public lab and
fleet API is :mod:`otto.lab`, with the lab model and its loader in
:mod:`otto.labs`. The repo accessors are read at :mod:`otto.bootstrap`, their public path;
:mod:`otto.declared` defines them.

.. automodule:: otto.config
