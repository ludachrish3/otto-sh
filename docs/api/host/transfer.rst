host.transfer
=============

.. _recursive-transfers:

Recursive transfers
--------------------

``put`` and ``get`` transfer a directory tree when called with
``recursive=True`` (``-r`` / ``--recursive`` on the CLI). Without the flag,
a directory source handed to ``put`` is refused by name (the error names
the offending path), on every backend, before any byte moves; ``get``
cannot cheaply stat a remote source up front, so a directory handed to a
non-recursive ``get`` fails with the backend's own error instead. The
rules, stated once here and linked from everywhere else:

* **Layout is ``cp -r``'s.** A source directory lands *under* ``dest_dir``
  wearing its own name: ``put(Path("build"), Path("/opt"), recursive=True)``
  produces ``/opt/build/...``. There is no rsync trailing-slash rule; to
  transfer a directory's contents, pass the contents.
* **The result is nested.** The top-level ``value`` is keyed by each source
  exactly as passed. A directory entry's own ``value`` maps each file's path
  *relative to that directory* to its per-file outcome; the entry's status
  is the first non-ok among them (``Skipped`` counts as ok), the same rule
  the top level uses. A missing or unreadable source, a failed remote
  listing, or a skeleton that could not be created is one error entry with
  an empty mapping.
* **Empty directories are created.** ``mode`` applies to files only —
  directories keep ``mkdir``'s default bits. ``user`` means exactly what it
  means for a file transfer, and the directories are created by that same
  identity — as ``user`` when one is given, so the skeleton is owned by
  whoever lands the files; by ``root`` inside a container; by the
  connection's login identity otherwise. A bad ``mode`` is refused before
  any directory is created, on every family.
* **Symlinks.** A symlink to a directory *inside* the tree is skipped
  entirely — neither recreated nor descended; a symlink to a file is
  copied by content (its target's bytes). A symlink passed as a top-level
  source is followed.
* **Names containing a newline.** On ``get``, a name containing a newline
  refuses the whole tree by name — the remote listing is line-oriented.
  ``put`` has no such rule; its local walk needs no line-oriented listing.
* **Dry run.** ``put`` previews the destination of every file (the local
  walk asks the host nothing; an empty directory has nothing to preview);
  ``get`` reports one ``NotRun`` entry per source, since enumerating a
  remote tree would be a command.
* **Embedded hosts** refuse ``recursive=True`` with ``NotImplementedError``.

Recursion is implemented once, above every transfer backend, in each
direction: ``put`` reduces a tree to one batched remote ``mkdir -p`` plus
the ordinary per-level puts; ``get`` issues one listing command per source,
creates the local skeleton with an ordinary ``mkdir``, then runs the
ordinary per-level gets. Every unix backend (``scp``, ``sftp``, ``ftp``,
``nc``, ``shell``) and every POSIX family carries it with its own progress
bars, ``mode`` batching and ``user`` handling unchanged.

.. _concurrent-transfers:

Concurrent transfers
--------------------

``put`` and ``get`` move the files of one batch concurrently by default
(``concurrent=True``; ``--concurrent`` on the CLI) and one at a time with
``concurrent=False`` (``--no-concurrent``). The rules, stated once here:

* **The flag is an in-flight count, nothing more.** With ``concurrent=True``
  a backend may have up to its protocol's ``max_concurrent_transfers`` files
  in flight; with ``False`` exactly one. Every source is attempted in both
  modes and answers its own entry -- no source is ``Skipped`` because a
  sibling failed. The only ``Skipped`` a transfer produces is a sibling of a
  directory handed to a non-recursive ``put`` (:ref:`recursive-transfers`).
* **The cap is per protocol and per connection.** ``scp_options``,
  ``sftp_options`` and ``nc_options`` each carry ``max_concurrent_transfers``;
  ``None`` derives a bound that fits a default OpenSSH server, described in
  :ref:`the host-options guide <transfer-channel-budget>` (``null`` when you
  spell it in lab data). ``shell``, ``ftp``,
  the embedded ``console`` and ``tftp``, and the local copy carry one file at
  a time; they accept ``concurrent=True`` and run one at a time.
* **One budget per transfer object.** Overlapping calls on one host -- the
  levels of a recursive transfer, or your own ``asyncio.gather`` of several
  ``put`` calls -- share the object's semaphore, so together they never
  exceed the cap. Three hosts each receiving a tree run three independent
  budgets, and a ``user=`` transfer is its own object on its own SSH
  connection with a budget of its own. The scp/sftp default deliberately
  claims only half the connection's usable channels: the other half is left
  for whatever else you run on that connection while the batch is in flight
  -- your own exec calls, a second transfer on another backend, an
  interactive session.
* **Containers** forward ``concurrent`` to the staging leg through the parent
  host; the ``docker cp`` leg is one command per file. Every file that
  staged is copied; a failed ``docker cp`` is that file's entry only.
* **Trees.** With ``concurrent=True`` every directory level's transfer is
  launched together and the backend's cap bounds the whole tree; with
  ``False`` levels and files go one at a time.
* **Cancellation stops the batch.** A cancellation reaching a transfer --
  a Ctrl-C, an ``asyncio.timeout`` -- cancels the files in flight and waits
  for each to finish cleaning up before it propagates, so nothing is left
  running behind it. No entry is fabricated for a cancelled file and nothing
  is retried; with ``concurrent=False`` the files not yet reached are simply
  never started.
* **Dry run** is unchanged: every entry is ``NotRun`` before any dispatch.

Two behaviours changed when this landed: ``scp`` and ``sftp`` batches larger
than the cap now queue instead of failing past a default server's
``MaxSessions``; and ``ftp``, ``shell``, ``console`` and local batches no
longer stop at the first failure.

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
