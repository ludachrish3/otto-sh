host.transfer
=============

.. ProgressGranularity is defined in otto.host.transfer.base and is documented
   there, on its own submodule page. That is the shape docs/api/link.rst and
   docs/api/tunnel.rst already use -- each excludes its re-exported members
   from the PACKAGE automodule and keeps them on the SUBMODULE page -- and it
   is also what -W requires here. Documenting the class at both paths gives the
   bare ``ProgressGranularity`` in its own type annotation two targets, and
   Sphinx's fuzzy resolution of an unqualified name then reports "more than one
   target found", which -W turns into a build failure.

.. automodule:: otto.host.transfer
   :exclude-members: ProgressGranularity
