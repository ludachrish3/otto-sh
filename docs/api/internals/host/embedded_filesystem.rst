embedded_filesystem internals
=============================

The names ``otto.host.embedded_filesystem`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/host/embedded_filesystem`.

.. currentmodule:: otto.host.embedded_filesystem

.. autoclass:: otto.host.embedded_filesystem.NoFileSystem

.. autoclass:: otto.host.embedded_filesystem.FatRamFileSystem

.. autoclass:: otto.host.embedded_filesystem.LittleFsFileSystem

.. autodata:: otto.host.embedded_filesystem.FILESYSTEM_CLASSES

.. autofunction:: otto.host.embedded_filesystem.build_filesystem
