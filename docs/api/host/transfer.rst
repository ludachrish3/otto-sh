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
