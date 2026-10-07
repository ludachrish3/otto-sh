API reference
=============

The reference has two halves. **Public API** documents each namespace otto
declares: a name is public if and only if it is in one of these modules'
``__all__``, and its import path survives otto's internal moves. Before 1.0
every public name is provisional; :doc:`stability` says what that promises.
**Internals** documents the rest of otto's modules, for contributors and for
the links the guides use to explain behaviour. Nothing there is a promise.

.. toctree::
   :caption: Public API
   :maxdepth: 1

   stability
   otto
   lab
   config
   bootstrap
   context
   host/index
   labs
   models/index
   link
   tunnel
   docker/index
   reservations
   inventory
   creds
   monitor/index
   coverage/index
   suite/index
   project
   session
   logger
   testing
   init/index
   instructions
   result
   errors
   utils
   registry
   params
   tls
   cli/registry
   examples

.. toctree::
   :caption: Internals
   :maxdepth: 1

   internals/index
