Adding a new embedded OS
========================

otto's embedded-host support is built around a small **framing seam** in
:class:`~otto.host.session.ShellSession`. Everything that differs between one
shell-bearing OS and another — how a command is wrapped to send, how its exit
code is recovered, what the unknown-command echo looks like — lives behind a
fixed set of seven overridable hooks. Everything else (the read loop,
``expect`` handling, timeout recovery, the session lifecycle) is shared.

This page is the bring-up guide for adding a new embedded OS class
(NuttX, U-Boot, MicroPython, a vendor shell — whatever speaks bytes over a
transport otto can drive).

The framing seam
----------------

Subclass :class:`~otto.host.session.TelnetSession` (or whichever transport
applies) and override the hooks that differ for your shell. The bash forms
in :class:`~otto.host.session.ShellSession` and the Zephyr forms in
:class:`~otto.host.zephyr.ZephyrSession` are the two reference
implementations to look at side-by-side.

.. list-table::
   :header-rows: 1
   :widths: 28 36 36

   * - Hook
     - bash (``ShellSession``)
     - Zephyr (``ZephyrSession``)
   * - ``_handshake_command()``
     - ``stty -echo 2>/dev/null; echo <READY>\n``
     - ``<READY>\n`` (unknown token; shell's error handler echoes it back)
   * - ``_frame_command(cmd)``
     - ``echo "<BEGIN>"; <cmd>; echo "<END>$?__"\n``
     - ``<BEGIN>\r<cmd>\rretval\r<END>\r`` (four CR-separated lines)
   * - ``_recover_command()``
     - ``echo <RECOVER>\n``
     - ``<RECOVER>\n``
   * - ``_marks_begin(data)``
     - Line equals or ends with ``<BEGIN>``
     - ``<BEGIN>`` is a *substring* of ``<token>: command not found``
   * - ``_extract_retcode(buffer)``
     - Digits captured by the END pattern ``__OTTO_<id>_END__(\d+)__``
     - Last bare ``-?\d+`` line before the END token (signed errno)
   * - ``_parse_output(buffer, cmd)``
     - Region between BEGIN and END markers, stripped of CRs
     - Purely positional ``[prompt, output, prompt]`` slice
   * - ``_end_pattern`` (class attr)
     - ``re.compile(r"__OTTO_<id>_END__(\d+)__")``
     - ``re.compile(r"__OTTO_<id>_END__")`` (no retcode capture)

The base class :meth:`~otto.host.session.ShellSession._ensure_initialized`
and :meth:`~otto.host.session.ShellSession._run_cmd_inner` call these hooks
at fixed points in the read loop, so a subclass that gets the seven hooks
right inherits the full engine for free — no read loop, no expect handler,
no recovery code to maintain.

Wiring the subclass through to a host
-------------------------------------

The :class:`~otto.host.session.SessionManager` takes a
``telnet_session_cls`` argument that the telnet dispatch path uses when
constructing a new session. Pass your subclass there from the embedded
host class:

.. code-block:: python

   class MyEmbeddedHost(RemoteHost):
       def __post_init__(self) -> None:
           # ... ConnectionManager + RepeatRunner wiring ...
           self._session_mgr = SessionManager(
               connections=self._connections,
               name=self.name,
               log_command=self._log_command,
               log_output=self._log_output,
               telnet_session_cls=MyShellSession,    # <- your subclass
           )

The ``(reader, writer, _owned_client)`` constructor signature of
:class:`~otto.host.session.TelnetSession` is what
:class:`~otto.host.session.SessionManager` calls, so your subclass needs to
preserve it (call ``super().__init__(reader, writer, _owned_client)``).

The DEBUG-driven bring-up loop
------------------------------

When a new shell doesn't quite cooperate — different unknown-command error
wording, different ``retval``-equivalent output, different ANSI noise — the
bring-up loop is *not* "read the source." It's:

1. Set DEBUG-level on the ``otto.host.*`` logger (standard Python logging —
   no env var, no custom filter):

   .. code-block:: python

      import logging
      logging.getLogger("otto.host").setLevel(logging.DEBUG)

   Or from the otto CLI's verbose flag. Or from a pytest run with
   ``log_level = "DEBUG"`` in :file:`pyproject.toml`.

2. Run a single command against the new target:

   .. code-block:: python

      host = MyEmbeddedHost(ip="...", ne="...", ...)
      await host.run("a command your shell has")

3. Read the log. The base ``ShellSession`` instrumentation logs the call
   sites of every hook, so without writing any per-subclass logging code
   you see:

   - ``MyShellSession@<id>: handshake start cmd=... marker=... timeout=...s``
   - ``MyShellSession@<id>: handshake matched in N.NN s (attempts=K, B bytes): '...'``
     — the bytes that came back are visible; the marker should be in the tail.
   - ``MyShellSession@<id>: framed write cmd='...' payload='...'``
   - ``MyShellSession@<id>: begin marker matched on chunk='...'`` — if this
     line *never* fires, ``_marks_begin`` is the hook to adjust.
   - ``MyShellSession@<id>: run_cmd done cmd='...' retcode=N output_len=L buffer='...'``
     — the full buffer ``_extract_retcode`` and ``_parse_output`` saw.

4. If a hook's slice is wrong, the buffer in step 3 shows you *exactly* what
   data the parser had to chew on. Iterate on the hook until the parse is
   right. The other six hooks are unaffected.

5. On a handshake that never completes, you'll see one or more
   ``handshake probe #K timed out, resending`` lines followed by
   ``handshake FAILED after K attempt(s)`` and a ``ConnectionError``. The
   most common cause is the marker arriving with leading bytes (ANSI codes,
   non-newline framing) that the readiness regex doesn't absorb — fix it in
   the regex, not by writing more glue.

Each subclass+session is tagged ``<class>@<session_id>`` in every log line,
so running multiple instances concurrently (e.g. the multi-config Zephyr
test bed in ``tests/firmware/zephyr/configs/``) produces a single
demultiplexable log.

What you do NOT need to do
--------------------------

- **Don't add per-subclass logging.** The base class already logs at the
  call site of every framing hook; your subclass inherits visibility for
  free. Per-subclass logging would just be noise.
- **Don't write a read loop.** The base ``_run_cmd_inner`` handles the
  pattern-match-or-newline loop, expect responses, and timeout recovery
  uniformly.
- **Don't add an env-var verbosity dial.** Standard Python logging at
  DEBUG is the contract — users opt in through any of the three normal
  paths (CLI, ``logging.getLogger``, pytest config).
- **Don't modify the target's firmware.** otto's design contract is that
  the target is met as-is — any sentinel/marker behavior the framing
  relies on must come from what the shell *already* does (e.g. Zephyr's
  unknown-command error echo, or a builtin like ``retval``). If the
  target doesn't provide enough behavior to frame a command, the
  appropriate response is a clear capability error, not a custom shell
  command added to the firmware.
