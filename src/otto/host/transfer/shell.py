"""Shell-only file transfer backend for UnixHost -- chunked base64 or uu over exec.

Moves files using nothing but command execution: no ``scp``, no ``nc``, no
``rsync``. Built for the devices at the bottom of the BusyBox matrix, whose
entire toolkit is a POSIX shell plus whatever applets are actually present --
including a BusyBox build old enough to lack ``base64`` entirely, which is
what the second codec is for.

TWO LAYERS, AND THE SPLIT IS THE POINT. ``ShellFileTransfer`` owns the
STAGING SKELETON -- name a temp in the destination's own directory and inside
the filename budget, fill it, verify it BEFORE the rename, rename it, clean
up on any failure AND on an interrupt (``ShellFileTransfer._cleanup_temp``,
``ShellFileTransfer._cleanup_temp_interrupted``) -- and a
:class:`ShellCodec` owns the FILLING, whole chunk
loop and all. There are two: :class:`Base64Codec`, which does exactly what
this module always did, and :class:`UuencodeCodec` for the rows with no
``base64`` applet. Which one a transfer gets is
``ShellFileTransfer._select_codec``'s answer, read from the device's own
probe -- never an operator's choice -- and base64 wins wherever it is
available. The codec's unit is the whole chunk loop rather than an
``encode()`` call because the two encodings need OPPOSITE loop orders: see
:class:`ShellCodec` for the measurement that settled that.

PUT chunks the local file into plaintext pieces of at most
``_SHELL_CHUNK_BYTES`` -- fewer where the host's exec path meets a line
discipline that would truncate the command; see that constant's note --
base64-encodes each locally, and appends it to a same-directory temp
file via ``printf '%s' '<chunk>' | base64 <flag> >> <temp>``; once every
chunk has landed, an integrity check runs against the TEMP (see below), and
only then does the last step move the temp onto the real destination with
``mv``, so a transfer that dies -- or fails its integrity check -- partway
through never leaves a truncated or wrong file at the real path. See
``ShellFileTransfer._put_one`` for the skeleton and
``Base64Codec.send_chunks`` for the loop.

GET is the mirror image with the encode/decode roles swapped, and the
staging moved from the remote side to the local one: the device is asked for
the file's size (:attr:`~otto.host.userland.Userland.stat_size`), then for
each ``_SHELL_CHUNK_BYTES``-byte range via
``dd if=<src> bs=<N> skip=<k> count=1 2>/dev/null | base64`` -- the device
only *encodes*; every decode runs locally, in Python, chunk by chunk. Bytes
land in a same-directory local temp (``<dest>.otto-<unique>``, exactly
PUT's naming); the same integrity check PUT runs then confirms the temp
against the remote source, and ``Path.replace()`` swings it onto the real
destination only once that check passes. See
``ShellFileTransfer._get_one`` for the skeleton and
``Base64Codec.fetch_chunks`` for the loop.

Both directions verify the same way, in
``ShellFileTransfer._verify_integrity``:
:attr:`~otto.host.userland.Userland.checksum` resolving to ``"md5sum"``
means a device-side ``md5sum`` compared against a digest accumulated locally,
chunk by chunk, as bytes are sent or decoded; resolving to ``"absent"``
means a byte-size comparison instead, via the same
:attr:`~otto.host.userland.Userland.stat_size` capability GET's own sizing
already reads. Either way, a mismatch is a failed transfer with the temp
removed -- never a warning, and never a ``Status.Success`` with a note.

Registers ``shell`` into the shared transfer registry on import, and is the
``busybox`` OS profile's default ``transfer`` (``otto.host.os_profile.
_register_builtin_os_profiles``).
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import shlex
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..userland import Userland

from ...result import CommandResult, Result
from ...utils import Status
from ..errors import UnsupportedOnUserlandError
from ..userland import APPLET_ABSENT, applet_capability
from .base import (
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
    mark_skipped,
)
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)

_SHELL_CHUNK_BYTES = 4096
"""CEILING on the plaintext bytes per chunk; base64 expands this to 5464 characters.

The size a chunk actually takes wherever nothing bounds the command line, and
an upper bound everywhere else: :meth:`Base64Codec.send_chunks` reads a
:attr:`PutChunkLoop.line_budget` when its transport has one and reads less
than this. Never MORE than this, whatever the budget -- a roomy transport is
not a reason to re-open a size that four separate limits below were measured
against.

Conservative, and the margin is MEASURED rather than assumed on both of the
two channels this backend can get. FOUR limits, in the order they were ruled
in or out.

ARG_MAX IS NOT THE CONSTRAINT. Measured device-side: a single exec argument
of >=4 MB succeeds inside a BusyBox-only chroot, nowhere near a chunk
command's line.

THE EXEC CHANNEL CARRIES A FULL CHUNK WITH ROOM TO SPARE, which is what Phase
5 added and what an earlier version of this note called UNMEASURED and
plausibly negative. Real chunk commands were put through a real dropbear over
a real ssh exec channel, and both halves of the inequality measured: the
channel takes 9000 characters intact and breaks at 9001 (re-measured one
character at a time), while the longest line this backend emitted for a full
chunk measured 5535. Roughly 3465 characters of headroom, positive rather
than negative. Note what that 5535 is attached to: the staged temp's whole
path is interpolated into every chunk command, so it was that rig's own
destination (27 characters) and a longer path spends the headroom -- what
matters is the RELATIONSHIP, not either number.

THAT MEASUREMENT NO LONGER HAS A TEST, AND CANNOT GET ONE BACK HERE. The rig
was a dropbear this repo grafted onto a BusyBox-only chroot, retired with the
rest of the artifact harness; the live BusyBox bed cannot replace it, because its
guests have no sshd at all by construction -- which is the point of the
ssh-shaped true negative they pin. What survives is hostless: the emitted
line lengths are computed and bounded by
``tests/unit/host/transfer/test_shell_transfer.py``'s
``TestShellChunkLineLength``. So after the harness's deletion NO test puts a
full-size chunk command on a real ssh exec channel, and a regression in that
direction would be caught by arithmetic rather than by a device.

THE PTY PATH IS THE ONE WITH NO MARGIN, and it is a separate measurement this
branch also holds. BusyBox ash's line editor
(``CONFIG_FEATURE_EDITING_MAX_LEN``) delivers 1022 characters intact and
truncates at 1023 -- the ``run-command-line-length`` record in
:data:`~otto.host.userland.GAPS`, measured identically against dropbear,
against OpenSSH and against a bare local pty, which is what identifies it as
the DEVICE's line editor rather than as any transport. A 5535-character chunk
line is over that by a factor of five.

WHICH OF THE TWO THIS BACKEND GETS IS THE HOST'S ``term``, not this module's
choice. ``_exec_cmd`` is ``UnixHost.exec``, and ``SessionManager.exec`` opens
a bare pty-less exec channel only for ``term: "ssh"``; telnet has no
stateless exec primitive, so it routes through a pooled SHELL session
instead -- the line-edited path. A ``term: telnet`` BusyBox host is therefore
exactly the case this note used to worry about, and it is NO LONGER
UNMEASURED: five telnet-console BusyBox guests joined the bed, the case
happened, and the paragraphs below are what they answered.

THE PTY PATH IS MEASURED NOW, AND IT WEDGED. On the bed's guests (2026-08-21,
``tests/integration/busybox_bed``) a PUT of more than ~2.8 KB never completes:
bisected on the 1.35.0 guest, 2800 bytes of payload succeeds in 0.1 s and
3000 bytes fails after 30.2 s, as do 4096, 8192, 16384 and 262144. That is
otto's own command timeout expiring, and the tail of the error carries the
guest's ``>`` -- ash's PS2 continuation prompt, waiting for the rest of a
single-quoted blob that never arrived. Exactly the failure this note
predicted, PS2 included, and the backstop predicted with it also held: the
staged temp is never renamed, so nothing landed wrong bytes under a
``Status.Success``.

THE CEILING IS THE KERNEL'S, NOT ASH'S, on these guests. A raw
``echo <pad> | wc -c`` probe -- counting what ARRIVED, so truncation reads as
a short count rather than as a pass -- carries 500, 1000, 1022, 1023, 1100,
2000 and 4000 characters intact and never returns at 4090. So the 1022 line
editor above is measurably INACTIVE here (1023 would have been the tell) and
what bounds the line is N_TTY's 4096-byte canonical buffer:
:data:`~otto.host.session.PTY_TYPED_LINE_MAX`. Both ceilings are real on
their own devices, neither subsumes the other, and
:func:`~otto.host.session.typed_line_budget` therefore hands out the smaller
of the two that apply to a given host.

THE RESPONSE IS A BUDGET, NOT A SMALLER CONSTANT AND NOT A DIFFERENT CODEC.
``exec``'s route decides whether a budget exists at all (``None`` on the ssh
channel, so its chunk lines stay byte-identical -- the path that was never
broken does not move), and :meth:`Base64Codec.send_chunks` spends whatever it
is handed by MEASURING its own empty command line and giving the payload what
is left, rounded down to the largest whole base64 quantum. On the bed that is
a 2889-byte chunk against the 4096 an ssh host still sends. Two properties
this shape has that a smaller universal ``_SHELL_CHUNK_BYTES`` would not: it
costs the ssh path nothing, and it survives a long destination path, which is
interpolated into every chunk command and is what the ssh-side headroom above
is also spent on.

THE ALTERNATIVE WAS PREFERRING UUENCODE ON PTY-ROUTED HOSTS, and it lost on
three counts even though it is measured green: the 1.16.1 guest, which has no
``base64`` applet, moves 256 KiB over the same telnet console in 3.7 s
because uu's payload rides a HEREDOC whose body lines are at most 61
characters. That measurement is why the budget is the right lever rather than
evidence for switching codecs -- it shows the pty path bounds the LINE and not
the command (uu's chunk command is ~5952 characters over 100 lines and crosses
fine, where base64's 5535 on ONE line does not), which is precisely what
sizing the line fixes. Against it: a pty-routed device with ``base64`` but no
``uudecode`` would still wedge, so the defect would survive the fix; the
preference base64 wins on measurement (:meth:`ShellFileTransfer._select_codec`)
would be overridden by a fact about the TRANSPORT rather than about the
device; and codec selection would acquire a second input, on a seam whose
whole point is that one function answers it from the device's own probe.
uu needs no budget of its own for the same reason it was a candidate: its
longest line does not grow with the chunk, so there is nothing for a budget to
size (its command's first line does carry the paths twice -- see
:class:`UuencodeCodec` -- and a destination long enough to push THAT over the
budget is a case no measurement covers and this note does not claim).

GET IS UNAFFECTED AND IS NOT BUDGETED. It sends a short ``dd | base64``
command and receives long OUTPUT, which no line discipline bounds: 16 KiB in
0.4 s and 256 KiB in 2.9 s on the same guest that could not PUT 3000 bytes.

THE ``run()`` GUARD DOES NOT REACH HERE, AND THAT IS THE POINT.
:func:`otto.host.session.refuse_if_line_editor_would_truncate` now refuses an
over-long typed line on an ash host, reading the same
``run-command-line-length`` record -- but it is called from
``SessionManager.run_cmd``, which is ``Host.run()``'s per-command path, and
NOT from ``ShellSession.run_cmd``, which is where the pooled exec session
above would meet it. Pushed one layer down it would refuse every chunk
command on a telnet or proxied-login BusyBox host rather than transfer it,
turning the bounded, loud, verified-against failure described below into a
hard block on the backend those devices depend on. Pinned by
``tests/unit/host/test_run_line_length.py``'s ``TestTheRefusalIsScoped``.

What that would cost was called bounded here before anything had measured it,
and the bed then measured it: the failure IS loud. Truncation lands inside the
single-quoted base64 blob of ``printf '%s' '<b64>' | base64 -d >> <temp>``, so
the far side gets an unterminated quote rather than a valid shorter command --
it errors or wedges, and on the guests it wedged, visibly, at PS2. It cannot
append plausible-looking bytes. That is the opposite of the gap record's
general case, where truncation silently runs a shorter command that works. And
the backstop holds regardless, which the same run confirmed: PUT verifies the
TEMP (see :meth:`ShellFileTransfer._verify_integrity`) before the ``mv``, so no
truncation anywhere can land wrong bytes at the destination under a
``Status.Success``.

None of that is an argument for leaving the line over the bound now that the
budget exists -- a loud failure is still a failed transfer. It is the reason
the ANSWER is a budget rather than a refusal, and the reason a truncation that
somehow gets past the budget still cannot corrupt a destination.
"""

_STAGING_TOKEN_HEX = 8
"""Hex characters of :func:`uuid.uuid4` kept in a staged temp's name.

32 bits, taken from the FIRST 8 characters of ``uuid4().hex`` -- which are
the whole of the ``time_low`` field and so entirely random, since uuid4
spends its only non-random nibbles on the version (hex index 12) and
variant (hex index 16). Truncating there costs no entropy beyond the
truncation itself.

Was the full 32-character hex. Shortened because the staged basename is
:meth:`~otto.host.transfer.base.BaseFileTransfer.put_files`'s problem, not
merely a cosmetic one: that method validates the CALLER's basename against
``max_filename_len`` and then stages under a LONGER name, so the check that
exists to turn "this target cannot open that name" into a clear up-front
refusal was answering for the wrong name. See :func:`staged_temp_name`,
which is what actually closes that gap -- this constant only makes the
budget it enforces cheap enough to spend on almost every real name. It also
buys 24 characters of transport headroom on every chunk command line, since
the staged path is interpolated into each one (measured over the retired
dropbear rig's real exec channel: 5558 characters per full chunk before, 5534
after).

Collision risk, deliberately re-checked rather than waved through: two
stagings collide only if they draw the same 32-bit token AND target the
same directory AND the same destination basename AND overlap in time.
Neither ``_run_put`` nor ``_run_get`` has any concurrency of its own --
both are strictly sequential, one-file-at-a-time loops (see their
docstrings), and each temp is renamed or unlinked away before the next file
starts -- so a single transfer can never race itself; it takes two
independent transfers aimed at one directory to get two live temps at all.
At that scale the birthday bound is n^2 / 2^33, i.e. about 1.2e-4 even for a
thousand simultaneous stagings of the same filename, against a failure mode
that is loud rather than silent: PUT verifies the TEMP (see
:meth:`ShellFileTransfer._verify_integrity`) before the ``mv``, so a
clobbered temp fails its own check instead of landing wrong bytes at the
destination.
"""

_INTERRUPTED_CLEANUP_TIMEOUT = 2.0
"""Seconds an INTERRUPTED put may spend removing its staged temp before giving up.

Bounds :meth:`ShellFileTransfer._cleanup_temp_interrupted` and nothing else.
The ordinary failure paths keep the host's own
:data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT` (30 s), which is the right
bound when nothing is racing them; the interrupted path runs inside the
graceful window an interrupt promises (``otto.lifecycle`` runs teardown
under a 10 s ``DEFAULT_TEARDOWN_DEADLINE``), where 30 s is not a bound at
all -- one hung ``rm`` on a session that is itself dying would hold teardown
three times past the deadline otto just told the operator about, which is a
worse bug than the leak this cleanup exists to close.

TWO SECONDS IS ~45x THE MEASURED COST, on the slowest transport this backend
has. On the bed's ``bb1350`` guest -- telnet through the ``test1`` hop into
a QEMU guest -- a warm-session ``rm -f -- <temp>`` round trip measured
42-45 ms over seven consecutive samples (2026-08-21), identical whether the
temp existed (64 KiB) or not; an ``ssh`` exec channel is faster still. The
expensive part of that path is the LOGIN (0.57 s, measured in the same run)
and this cleanup never pays it: it rides the session the transfer was already
using. So the bound cannot truncate a healthy cleanup, and an unhealthy one
costs a fifth of the teardown window instead of all of it -- the remaining
~8 s is what the session teardown behind it still needs.

At most one temp is ever cleaned up per interrupt, whatever the batch size:
:meth:`ShellFileTransfer._run_put` is strictly sequential, so exactly one
file is in flight when the cancellation lands.
"""


def staged_temp_name(dest_name: str, max_filename_len: int) -> str:
    """Name a same-directory staging temp for *dest_name* the target can actually open.

    Returns ``<dest_name>.otto-<token>``, with *dest_name* truncated by as
    much as it takes for the whole basename to fit inside *max_filename_len*
    -- the caller's declared cap on a name the target's filesystem will
    accept (:attr:`~otto.host.remote_host.RemoteHost.max_filename_len`).

    The truncation is the point, not a rounding detail.
    :meth:`~otto.host.transfer.base.BaseFileTransfer.put_files` and
    :meth:`~otto.host.transfer.base.BaseFileTransfer.get_files` both validate
    the caller's basename against exactly this number, up front, precisely so
    that an over-long name is a clear refusal instead of an opaque device
    error partway through -- and then both hand that basename to a staging
    step that makes it longer. A name at the cap therefore passed the front
    door and failed at the back one: measured on this repo's ext4 checkout
    (``NAME_MAX`` 255), a 255-character source staged as a 293-character temp
    and PUT died with ``/bin/sh: cannot create ...: File name too long``
    after the first chunk had already crossed the wire, GET with
    ``OSError: [Errno 36]`` from its local temp. Shortening the token alone
    does NOT fix that -- 255 + len(".otto-") + 8 is 269, still over 255 --
    which is why this budget exists and not just :data:`_STAGING_TOKEN_HEX`;
    the short token is what keeps the budget from biting any name shorter
    than ``max_filename_len - 14``.

    Truncating the DESTINATION half rather than the token keeps the random
    part whole: the token is what makes two concurrent stagings distinct (see
    :data:`_STAGING_TOKEN_HEX`), whereas the retained prefix only has to make
    a stray temp traceable back to its file by eye. Nothing ever reads the
    name back -- the temp path is carried in a local variable from creation
    through to ``mv`` / ``Path.replace()`` -- so a truncated prefix loses no
    information otto later needs.

    A *max_filename_len* below the token's own framing (14 characters) leaves
    no room for any of *dest_name*, and the final slice then keeps the result
    inside the cap at the cost of some of the token. No unix target in the
    BusyBox matrix declares anything remotely like that -- every one is the
    ``255`` default -- but a bound that silently stops holding below some
    threshold is worse than one that degrades visibly.

    ONE VALUE IS OUT OF CONTRACT, and it is named here rather than left to be
    rediscovered: ``max_filename_len=1`` returns ``"."``. That is inside the
    cap, so the bound holds arithmetically, but ``.`` is a basename resolving
    to the CONTAINING DIRECTORY -- the one degenerate output that is not
    visible degradation in the sense the paragraph above claims. Every limit
    from 6 up keeps a recognisable ``.otto-`` prefix and degrades honestly.
    Unreachable in production (*max_filename_len* is a declared host field, and
    no profile declares anything under ``255``) and left as-is deliberately:
    making it visible means raising, which is a behaviour change no caller has
    asked for. Pinned by
    ``test_a_limit_of_one_returns_a_dot_which_is_out_of_contract``, so a change
    of mind updates a test rather than surprising someone.
    """
    suffix = f".otto-{uuid.uuid4().hex[:_STAGING_TOKEN_HEX]}"
    keep = max(max_filename_len - len(suffix), 0)
    return f"{dest_name[:keep]}{suffix}"[:max_filename_len]


# ---------------------------------------------------------------------------
# The codec seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkLoopOutcome:
    """How a codec's chunk loop ended: how many chunks moved, and why it stopped.

    Returned rather than raised, matching
    :meth:`ShellFileTransfer._verify_integrity`'s own ``str | None`` shape:
    a codec decides whether its loop finished, never what a
    :class:`~otto.result.Result` should say.
    """

    chunks: int
    """Chunks the loop actually moved.

    Both directions read this, and for the same reason in mirror image:
    ``ShellFileTransfer._put_one`` creates its temp explicitly (``: > <temp>``)
    when a source produced no chunks at all, and
    ``ShellFileTransfer._get_one`` fires its one ``(0, 0)`` progress call when
    a remote file produced none. On a loop that stopped early this is the
    count reached BEFORE the failure, not the count planned -- the caller
    returns on *error* before reading it, so the two never disagree in
    practice.
    """

    error: str | None = None
    """``None`` if every chunk moved; otherwise why the loop stopped.

    A fragment, not a finished message: the caller prefixes it with the file
    it belongs to (``f"{src}: {outcome.error}"``) and turns it into a failing
    :class:`~otto.result.Result`, exactly as it does with
    :meth:`ShellFileTransfer._verify_integrity`'s return. A codec therefore
    names the codec-specific detail (which chunk, which command, what the
    device said) and nothing else.
    """


@dataclass(frozen=True)
class PutChunkLoop:
    """What a codec is handed to move one local file into an already-named remote temp.

    Everything here is the STAGING SKELETON's work, already done: the temp has
    been named inside the destination's own directory and quoted, and removing
    it, verifying it and renaming it onto the real destination all happen after
    the codec returns. A codec only fills it.
    """

    exec_cmd: "Callable[..., Coroutine[Any, Any, CommandResult]]"
    """The device's command channel -- ``ShellFileTransfer``'s own ``_exec_cmd``."""

    src: Path
    """The LOCAL source file, unopened.

    Handed as a path rather than as an open handle or a chunk iterator because
    the read size is the codec's to choose: it is a function of how far the
    encoding expands a chunk against the transport's line budget, and the two
    encodings expand differently (see :data:`_SHELL_CHUNK_BYTES`). An
    :exc:`OSError` raised while reading it propagates to the caller, which
    already catches one around the whole staging skeleton.
    """

    temp: Path
    """The staged remote temp, for MESSAGES -- never interpolate this into a command."""

    quoted_temp: str
    """:func:`shlex.quote`\\ d *temp*, which is what a command may interpolate."""

    on_sent: "Callable[[bytes], None]"
    """Report one chunk's PLAINTEXT bytes, once the device has accepted them.

    The running byte count, the local ``md5`` the integrity check compares
    against, and the progress handler all live on the shared side and are all
    fed from here -- so a codec never touches them, and never has to know that
    ``bytes_done`` is counted in plaintext rather than in encoded characters.
    Call it once per chunk, AFTER the chunk's command succeeded; a codec that
    calls it before has told the caller bytes landed that may not have.
    """

    line_budget: int | None = None
    """Characters this transport carries on ONE line of a command, or ``None``.

    ``None`` means no line discipline is in the way -- an ``ssh`` host's bare
    exec channel, or a caller-supplied exec primitive -- and a codec should
    then do exactly what it did before this field existed. It is not "unknown"
    and never a reason to guess: the number comes from
    :attr:`~otto.host.session.SessionManager.exec_line_budget`, which is the
    object that decides which primitive :meth:`~otto.host.session.SessionManager.exec`
    routes through, and it is already net of otto's own BEGIN/END framing, so a
    codec compares it against the command string it is about to emit and
    nothing else.

    WHY THE CODEC SPENDS IT RATHER THAN THE SKELETON. What a chunk command
    looks like -- how many lines, what fraction is payload, whether the payload
    affects the longest line at all -- is exactly what :class:`ShellCodec`
    exists to vary, and the two shipped codecs answer differently:
    :class:`Base64Codec` puts a whole chunk on one line and must shrink the
    chunk to fit, while :class:`UuencodeCodec`'s body lines are 61 characters
    whatever the chunk size, so no chunk size it could pick would spend this.
    A skeleton that converted the budget into a chunk size for both would be
    answering a question only one of them has.
    """


@dataclass(frozen=True)
class GetChunkLoop:
    """What a codec is handed to pull one remote file's bytes back, chunk by chunk.

    The local staging temp is already open on the shared side and is renamed
    onto the destination after the codec returns; *on_received* is the only
    way into it. The device only ENCODES for GET -- every decode is local
    Python -- so a codec's GET half is an emit-and-parse pair, where its PUT
    half is an emit-and-check one.
    """

    exec_cmd: "Callable[..., Coroutine[Any, Any, CommandResult]]"
    """The device's command channel -- ``ShellFileTransfer``'s own ``_exec_cmd``."""

    src: Path
    """The REMOTE source path, unquoted. A codec quotes it into its own commands."""

    total: int
    """*src*'s size in bytes, as the device just reported it.

    The chunk COUNT is derived from this by the codec, not passed in, for the
    same reason :attr:`PutChunkLoop.src` is a path: the read size is the
    codec's own.
    """

    on_received: "Callable[[bytes], None]"
    """Report one chunk's DECODED bytes.

    Writes them to the local staging temp and feeds the same byte count,
    local ``md5`` and progress handler :attr:`PutChunkLoop.on_sent` feeds.
    Raises :exc:`OSError` if the local write fails, which propagates to the
    caller's own ``except OSError``.
    """


class ShellCodec(ABC):
    """One way to move bytes through a device's shell, chunk loop and all.

    What is NOT here is as load-bearing as what is. The STAGING SKELETON stays
    with :class:`ShellFileTransfer`: naming the temp inside the destination's
    own directory and inside ``max_filename_len``'s budget, creating an empty
    one when the source has no bytes, verifying integrity BEFORE the rename,
    the rename itself, and cleaning up after any failure. That is
    codec-independent, and it is the part that was measured into shape.

    THE UNIT IS THE WHOLE LOOP, NOT AN ``encode()`` CALL, and that is a
    measurement rather than a taste. ``base64`` is a stream codec:
    otto appends encoded chunks to one remote temp and the device decodes the
    concatenation once, at the end. ``uuencode`` is a CONTAINER format: each
    chunk carries its own ``begin``/``end`` framing, so appending three
    uuencoded chunks and decoding the result once returns only the FIRST
    chunk -- 4096 of 10253 bytes, at rc=0, on all five rows of the BusyBox
    matrix (measured 2026-08-14; see
    ``todo/busybox-parity-sweep-2026-08-11.md``). What works there is the
    inverse order: decode each chunk on arrival and append the PLAINTEXT. A
    seam with one ``encode(chunk) -> str`` hook can express base64's order and
    cannot express uu's, so the hook is the loop.

    Concretely, that is what each method below is free to vary: how many
    commands one chunk costs, what those commands are, what a chunk even is,
    and in which order encoded and plaintext bytes reach the temp. What it may
    not vary is when :attr:`PutChunkLoop.on_sent` /
    :attr:`GetChunkLoop.on_received` are called -- once per chunk, after the
    bytes are safe -- because the shared side's integrity check is built from
    those calls.
    """

    @abstractmethod
    async def send_chunks(self, loop: PutChunkLoop) -> ChunkLoopOutcome:
        """Move every byte of ``loop.src`` into ``loop.temp`` on the device.

        Owns the read loop, the encoding, the commands and their order.
        Returns without renaming, verifying or cleaning up anything: a
        non-``None`` :attr:`ChunkLoopOutcome.error` leaves the temp exactly
        where it is and the caller removes it.
        """

    @abstractmethod
    async def fetch_chunks(self, loop: GetChunkLoop) -> ChunkLoopOutcome:
        """Pull every byte of ``loop.src`` back from the device, decoded.

        Owns the chunk count, the commands, and the local decode. Hands each
        chunk's plaintext to :attr:`GetChunkLoop.on_received` and never writes
        to the local filesystem itself.
        """


class Base64Codec(ShellCodec):
    """``base64`` over ``printf`` and ``dd`` -- the stream codec this backend was built on.

    PUT appends each chunk's encoded text to the temp with
    ``printf '%s' '<b64>' | base64 <flag> >> <temp>`` and lets the DEVICE
    decode; GET pulls ``dd ... | base64`` and decodes LOCALLY. Both halves are
    what ``tests/integration/busybox_bed/test_shell_codec.py`` round-trips on
    four live guests, and the emitted strings are pinned byte-for-byte by
    ``tests/unit/host/transfer/test_shell_transfer.py``'s
    ``TestShellEmittedCommandLinesArePinned``.

    *decode_flag* is whatever
    :attr:`~otto.host.userland.Userland.base64_flag` resolved to, emitted
    verbatim and never hard-coded: ``base64 --decode`` is rejected on every
    BusyBox row tested, and BusyBox's ``-d`` is not what every GNU host wants.
    It is held HERE, on the codec, rather than passed per call, because it is
    a knob of this encoding and of no other -- ``uuencode`` has no decode flag
    and needs an ``-o`` output path instead. GET never emits it at all (the
    device only encodes there), so a codec built for GET alone would still
    carry it unused; that is the price of one object per codec rather than one
    per direction, and it is cheaper than the alternative.
    """

    def __init__(self, decode_flag: str) -> None:
        self._decode_flag = decode_flag

    def _chunk_command(self, encoded: str, redirect: str, quoted_temp: str) -> str:
        """Build the one command line this codec emits for one chunk.

        Extracted for a second caller rather than for tidiness:
        :meth:`_fitted_chunk_bytes` sizes the payload by BUILDING this command
        with an empty one and measuring what is left over. Two copies of the
        shape -- one emitted, one estimated -- is the arrangement where a
        budget silently stops holding the day the command grows a character,
        and the estimate is the copy nothing would red.
        """
        return f"printf '%s' '{encoded}' | base64 {self._decode_flag} {redirect} {quoted_temp}"

    def _fitted_chunk_bytes(self, quoted_temp: str, line_budget: "int | None") -> int:
        """Plaintext bytes per chunk whose emitted command line fits *line_budget*.

        :data:`_SHELL_CHUNK_BYTES` when there is no budget, unconditionally --
        that is what keeps an ``ssh`` host's chunk lines byte-identical to what
        the dropbear rig measured.

        With a budget, the arithmetic is derivation and no constant.
        Everything the command spends that is not payload is MEASURED, by
        building :meth:`_chunk_command` with an empty payload and the LONGER of
        the two redirects (``>>``, one character more than the ``>`` the first
        chunk uses), so the first chunk lands one character under rather than
        every later one landing one over. What remains is what the base64 TEXT
        may cost, and base64 only takes lengths that are multiples of 4: ``n``
        plaintext bytes become ``4 * ceil(n / 3)`` characters, so the largest
        payload fitting ``a`` characters is ``3 * (a // 4)``, exactly. Rounding
        to a whole quantum also leaves every chunk's encoding padding-free,
        which is tidier than the 4096-byte chunk this replaces (4096 is not a
        multiple of 3) but is not required -- each chunk command decodes on its
        own, so padding mid-stream was always fine.

        A NON-POSITIVE ANSWER IS RETURNED, NOT CLAMPED. It means the staged
        path alone has eaten the whole line, so no chunk size exists that
        works, and clamping to 1 would emit the wedging line one byte at a
        time. The caller turns it into a failed file with both numbers in the
        message.
        """
        if line_budget is None:
            return _SHELL_CHUNK_BYTES
        available = line_budget - len(self._chunk_command("", ">>", quoted_temp))
        return min(_SHELL_CHUNK_BYTES, 3 * (available // 4))

    @override
    async def send_chunks(self, loop: PutChunkLoop) -> ChunkLoopOutcome:
        """Append a chunk at a time, encoded, and let the device decode.

        A chunk is :data:`_SHELL_CHUNK_BYTES` wherever the transport carries a
        whole one, and :meth:`_fitted_chunk_bytes` of it where it does not. The
        read size is decided ONCE per file, before the loop: everything it
        depends on -- the staged path, the decode flag, the budget -- is fixed
        for the whole file, and a per-chunk answer would only invite two
        answers to disagree.

        The first chunk uses ``>`` and every later one ``>>``, which is what
        makes an appending chunk's command line exactly one character longer
        than the first's. Successive ``base64 <flag>`` invocations appending
        to one file concatenate correctly and IN ORDER -- measured end to end on
        the four matrix rows that have a ``base64`` applet at all, by
        ``test_shell_codec.py::test_a_multi_chunk_payload_reassembles_in_order``
        in the live bed suite: otto emits one decode-into-append per chunk, so a
        byte-identical file across five chunks IS that order observed on a
        device.

        The literal single quotes around the encoded text need no escaping:
        :func:`base64.b64encode` emits ``[A-Za-z0-9+/=]`` only, never wraps,
        and so never produces a quote, another shell metacharacter, or a
        leading ``-``. ``printf '%s'`` rather than ``echo`` because that is
        the shape measured on-device at full chunk size; ``echo`` was only
        ever measured against a 4-character probe, and its escaping and
        ``-n`` handling are userland-dependent.
        """
        read_size = self._fitted_chunk_bytes(loop.quoted_temp, loop.line_budget)
        if read_size <= 0:
            empty = len(self._chunk_command("", ">>", loop.quoted_temp))
            return ChunkLoopOutcome(
                0,
                f"no chunk of {loop.temp} can be written: this host's exec path carries "
                f"{loop.line_budget} characters on one command line, and the chunk command "
                f"for an EMPTY payload is already {empty} -- the destination path is too "
                f"long to shell-transfer to this host. Nothing was sent",
            )
        sent = 0
        with loop.src.open("rb") as f:
            while True:
                chunk = f.read(read_size)
                if not chunk:
                    break
                encoded = base64.b64encode(chunk).decode("ascii")
                redirect = ">>" if sent else ">"
                result = await loop.exec_cmd(
                    self._chunk_command(encoded, redirect, loop.quoted_temp)
                )
                if not result.is_ok:
                    return ChunkLoopOutcome(
                        sent,
                        f"writing a chunk to {loop.temp} failed "
                        f"(exit {result.retcode}): {result.value or result.msg}",
                    )
                sent += 1
                loop.on_sent(chunk)
        return ChunkLoopOutcome(sent)

    @override
    async def fetch_chunks(self, loop: GetChunkLoop) -> ChunkLoopOutcome:
        """Ask ``dd`` for each block range, encoded, and decode it locally.

        No wrap flag (``-w 0`` or similar) is ever passed to the remote
        ``base64``, because none is portable and none is needed: whatever the
        device's encoder wrapped to is flattened here by stripping ALL
        whitespace before decoding, which is lossless against an alphabet that
        contains none. Measured, not assumed -- a full 4096-byte chunk comes
        back as 72 lines of up to 76 columns on every matrix row that has the
        applet.

        The flatten step is load-bearing rather than defensive.
        :func:`base64.b64decode` runs with ``validate=True``, which rejects any
        byte outside the alphabet -- including the newlines the device just
        emitted -- so the flatten has to run first. The default
        ``validate=False`` would instead DISCARD stray bytes silently, turning
        a wedged transport's leaked stderr or prompt fragment into wrong bytes
        with no error at all.
        """
        num_chunks = (loop.total + _SHELL_CHUNK_BYTES - 1) // _SHELL_CHUNK_BYTES
        quoted_if = shlex.quote(f"if={loop.src}")
        for k in range(num_chunks):
            result = await loop.exec_cmd(
                f"dd {quoted_if} bs={_SHELL_CHUNK_BYTES} skip={k} count=1 2>/dev/null | base64"
            )
            if not result.is_ok:
                return ChunkLoopOutcome(
                    k,
                    f"reading chunk {k} failed (exit {result.retcode}): "
                    f"{result.value or result.msg}",
                )
            try:
                decoded = base64.b64decode("".join((result.value or "").split()), validate=True)
            except ValueError as e:
                return ChunkLoopOutcome(k, f"chunk {k} was not valid base64: {e}")
            loop.on_received(decoded)
        return ChunkLoopOutcome(num_chunks)


_UU_BYTES_PER_LINE = 45
"""Plaintext bytes one ``uuencode`` body line carries. Fixed by the format.

45 is what every ``uuencode`` emits and what :func:`binascii.b2a_uu` accepts
as its maximum in one call, so this is a property of the encoding rather than
a knob: 45 bytes become a length character plus 60 encoded characters, the 61
measured on all five matrix rows (see :class:`UuencodeCodec`).
"""

_UU_FRAME_NAME = "otto"
"""The name baked into every emitted ``begin`` header.

Never read back by anything. ``uudecode`` would write to it, which is exactly
why :meth:`UuencodeCodec.send_chunks` always passes ``-o`` -- measured on all
five rows: with no ``-o``, ``uudecode`` exits 0 and creates a file called
``otto`` in the working directory instead. Constant rather than derived from
the destination so that no caller-supplied name reaches a device command by
this route; the frame rides a quoted heredoc body, so it could not be
interpreted anyway, and a constant makes that argument unnecessary.
"""

_UU_FRAME_MODE = "600"
"""The mode baked into every emitted ``begin`` header.

``uudecode -o`` APPLIES it -- measured, the scratch lands ``-rw-------`` on
all five rows -- so this is the SCRATCH's mode and nothing else's. It never
reaches the transferred file: the plaintext is appended to the staged temp
with ``cat``, which does not carry a mode, and the temp's own mode is whatever
the device's umask gave it. That is what dissolves the ``put --mode`` question
``todo/busybox-parity-sweep-2026-08-11.md`` raised against uu's header (BusyBox
emits ``begin 664`` for its own encodes): a header mode that only ever
describes a file otto deletes cannot disagree with anything.

600 rather than 664 because a scratch holding another file's plaintext should
not be world-readable for the moment it exists.
"""

_UU_HEREDOC_DELIMITER = "ottoUU"
"""Heredoc delimiter for a chunk's framed text. Provably unmatchable by a body line.

A quoted heredoc (``<<'ottoUU'``) makes the body LITERAL -- no expansion, no
escaping, and no way for a byte of the payload to end the command early --
which is the whole reason the uu chunk rides one rather than a quoted
``printf`` argument. uu's alphabet is 0x20-0x60 (it emits a backtick for the
zero value; see :func:`_uu_frame`), so an encoded line can and does contain
single quotes, backslashes and backticks, all of which a ``printf '%s'``
argument would have to escape.

That safety needs the delimiter to be a string no body line can equal, and
LOWERCASE is what guarantees it: 0x61-0x7a is outside the encoded alphabet
entirely, so no frame line -- full, short or terminating -- can ever be
``ottoUU``. Pinned by ``test_no_frame_line_can_ever_close_the_heredoc_early``.
"""


def _uu_frame(chunk: bytes) -> str:
    """Frame *chunk* as one self-contained ``begin``/``end`` uuencode container.

    THE CONTAINER IS THE POINT, and it is why this codec's loop is shaped the
    way it is rather than base64's way: each chunk is a WHOLE uu document that
    the device decodes on arrival. Concatenating several and decoding once
    returns only the first, silently and at rc=0 -- so the plaintext, never
    the framed text, is what accumulates in the staged temp. That is a
    NEGATIVE MEASUREMENT and it is kept as one:
    ``test_shell_codec.py::test_appending_uu_frames_and_decoding_once_truncates_at_rc_zero``
    builds frames with THIS function, puts them on each of the five live
    guests and reads back one chunk of the several it sent. See
    :class:`ShellCodec`.

    ``backtick=True`` so the zero value encodes as ``` ` ``` rather than as a
    SPACE, and that is a transport property rather than a style: with it, no
    line of the frame contains a space character at all (the length character
    is ``chr(0x20 + n)`` for ``n >= 1`` and every data character is
    ``chr(0x21..0x60)``), so nothing a transport might do to trailing
    whitespace can shorten a line and silently drop bytes from its decode. It
    also matches what BusyBox's own ``uuencode`` emits on the GET side --
    measured: backticks, no trailing spaces, on the real ssh channel.
    """
    lines = [f"begin {_UU_FRAME_MODE} {_UU_FRAME_NAME}"]
    for start in range(0, len(chunk), _UU_BYTES_PER_LINE):
        piece = chunk[start : start + _UU_BYTES_PER_LINE]
        lines.append(binascii.b2a_uu(piece, backtick=True).decode("ascii").rstrip("\n"))
    lines.append("`")
    lines.append("end")
    return "\n".join(lines)


def _uu_unframe(text: str) -> bytes:
    """Decode one ``begin``/``end`` container back to bytes, or raise ``ValueError``.

    STRICT, for the reason :meth:`Base64Codec.fetch_chunks` decodes with
    ``validate=True``: a lenient parse turns a wedged transport's leaked
    stderr, a login banner or a truncated reply into wrong bytes with no error
    at all. Three things are therefore required rather than tolerated -- a
    ``begin`` header, a zero-length terminator line, and an ``end`` -- and a
    reply missing any of them raises instead of returning what it managed to
    read. A truncated reply is the one that matters: it is the shape a real
    transport failure takes, and it is indistinguishable from a short file by
    length alone.

    THE LENGTH CHECK IS THE ``validate=True`` OF THIS DECODER, and it is not
    defensive padding. :func:`binascii.a2b_uu` SILENTLY ZERO-PADS a line whose
    characters run out before its length character says they should: measured,
    ``a2b_uu("NOTUU")`` returns 46 bytes -- 43 of them NUL -- and raises
    nothing, because ``N`` declares 46 and the four characters after it supply
    three. So a line clipped in transit decodes to plausible zeroed bytes at
    no error, which is exactly base64's ``validate=False`` failure in another
    costume. A correct line carries ``1 + 4 * ceil(n / 3)`` characters for its
    declared ``n``, and anything else raises here.

    It rejects a PADDED encoder too -- one that filled its final line out to
    full width, or spelled the zero value as a trailing SPACE the transport
    then stripped -- and that is deliberate rather than an accepted
    limitation. Both would be indistinguishable from truncation without a
    second measurement, and BusyBox's own ``uuencode`` measurably does
    neither: exact-width lines, backticks for zero, no trailing whitespace
    anywhere, on all five matrix rows and over the real ssh channel.

    Lines are matched after stripping a trailing CARRIAGE RETURN, because the
    device's text arrives through
    :meth:`~otto.host.session.SessionManager.exec`'s line rejoin; nothing else
    is stripped, since a correctly framed line contains no whitespace at all
    (see :func:`_uu_frame`).
    """
    decoded = bytearray()
    seen_begin = False
    seen_terminator = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not seen_begin:
            if line.startswith("begin "):
                seen_begin = True
            continue
        if line == "end":
            if not seen_terminator:
                raise ValueError("the frame reached `end` with no zero-length terminator line")
            return bytes(decoded)
        if seen_terminator:
            raise ValueError(f"the frame carried data after its terminator: {line!r}")
        if not line:
            continue
        # `& 0x3F` is the mapping `a2b_uu` itself applies, and it is what makes
        # the terminator line work: uu spells zero as a BACKTICK, whose raw
        # `ord - 0x20` is 64 rather than 0. A check that skipped the mask would
        # reject every well-formed frame's last line.
        declared = (ord(line[0]) - 0x20) & 0x3F
        expected = 1 + 4 * ((declared + 2) // 3)
        if declared > _UU_BYTES_PER_LINE or len(line) != expected:
            raise ValueError(
                f"line {line!r} declares {declared} bytes, which needs {expected} "
                f"characters, and carries {len(line)} -- a clipped line decodes to "
                f"zero padding rather than an error"
            )
        try:
            piece = binascii.a2b_uu(line)
        except binascii.Error as e:
            raise ValueError(f"line {line!r} is not uuencoded text: {e}") from e
        if not piece:
            seen_terminator = True
        else:
            decoded += piece
    if not seen_begin:
        raise ValueError("the reply carried no `begin` header")
    raise ValueError("the frame was never closed by an `end` line")


class UuencodeCodec(ShellCodec):
    """``uuencode``/``uudecode`` -- the container codec, for a device with no ``base64``.

    THE ROW THIS EXISTS FOR IS 1.16.1, the one BusyBox matrix artifact that
    ships no ``base64`` applet at all; ``uudecode`` and ``uuencode`` are
    present on all five (measured, ``test_applet_userland.py`` in the live bed
    suite). It is the SECOND choice wherever both are available -- see
    :meth:`ShellFileTransfer._select_codec` for why base64 is preferred.

    PUT DECODES PER CHUNK AND APPENDS PLAINTEXT, which is the inverse of
    base64's order and is forced by measurement rather than chosen. Each chunk
    is framed locally into a whole ``begin``/``end`` document and sent as ONE
    command::

        uudecode -o <scratch> <<'ottoUU' && cat -- <scratch> >> <temp>
        begin 600 otto
        M...                      (92 lines for a full chunk, <=61 characters
                                   each; 100 lines in the whole command)
        `
        end
        ottoUU
        otto_rc=$?
        rm -f -- <scratch>
        (exit $otto_rc)

    Four things in that shape are each a measurement, all re-taken for this
    task on all five rows in a BusyBox-only chroot and on a real ssh channel:

    ``-o`` IS MANDATORY, and it works on 1.16.1. Without it ``uudecode``
    writes to the name in the header and exits 0, so the plaintext lands
    somewhere otto never reads. 1.16.1 was the row this whole path depended on
    and the one where an option was most likely to be missing (``base64
    --decode`` and ``busybox --list`` both fail there); it accepts ``-o``
    identically to every later row.

    THE SCRATCH IS UNCONDITIONALLY REMOVED IN THE SAME COMMAND. A FAILED
    ``uudecode -o`` LEAVES THE FILE BEHIND -- measured: a garbage frame exits 1
    with ``uudecode: short file`` and the empty scratch is still there
    afterwards. That is why the ``rm -f`` is not chained onto the success path
    with ``&&``: it must run whether the decode worked or not. ``otto_rc``
    carries the real exit status across it, and ``(exit $otto_rc)`` republishes
    that status in a SUBSHELL rather than with a bare ``exit`` -- a bare one
    would terminate a pooled shell session rather than one command.

    A HEREDOC, NOT A QUOTED ARGUMENT, AND THE ALTERNATIVE IS NOT MERELY
    UGLIER -- IT CAN EXCEED THE TRANSPORT. uu's alphabet contains single
    quotes (the byte value 7 encodes to one), so the obvious one-line form,
    ``printf`` with each frame line as a quoted argument, has to escape them,
    and :func:`shlex.quote` turns every quote into four characters. Its length
    is therefore PAYLOAD-DEPENDENT: measured for one full chunk against the
    same paths, 6275 characters for an ordinary byte ramp and 11330 for a
    chunk of ``0x07`` bytes, whose encoding is all quotes -- past the ~9000
    the channel carries, for a chunk size that is perfectly safe otherwise. A
    quoted heredoc is literal, needs no escaping at all, and is a FIXED 5952
    characters whichever bytes it carries. See :data:`_UU_HEREDOC_DELIMITER`
    for why no body line can close it early, and
    ``test_the_commands_length_does_not_depend_on_the_payloads_bytes`` for the
    guard on that fixedness.

    THE CEILING IS ON THE WHOLE COMMAND STRING, NOT ON ITS LONGEST LINE, and
    that was measured here rather than inherited: a 400-line command of
    18-character lines (8991 characters) crosses dropbear's exec channel and
    one of 500 such lines (9009) drops the connection, which is the same
    ~9000-character boundary the retired dropbear rig measured one character
    at a time for a SINGLE line (see the module docstring on what that
    retirement cost). So the multi-line shape buys no room against that limit
    and is not claimed to: for a 26-character destination a full chunk's command
    is 5952 characters (5953 for an appending one) against base64's 5533,
    leaving about 3047 characters of headroom rather than base64's 3466. uu
    spends the difference on the scratch path, which appears TWICE in every
    chunk command, so a long destination spends this headroom about three
    times as fast as base64's does. What the shape does buy is a LONGEST LINE
    of 186 characters instead of 5533 -- under the 1022-character ash line
    editor bound the ``run-command-line-length`` gap records, though nothing
    measures a multi-line command on that path and this docstring claims
    nothing about it.

    GET IS THE EASY HALF, as it is for base64: the device only ENCODES, so
    ``dd ... | uuencode otto`` comes back as one frame per chunk and
    :func:`_uu_unframe` decodes it locally. The container problem does not
    arise, because the decode is local Python, one chunk at a time.

    *max_filename_len* is held on the codec for the same reason
    :attr:`Base64Codec._decode_flag` is: it is a knob this encoding needs and
    the other does not. base64 stages nothing on the device beyond the temp the
    skeleton already named; uu needs a SECOND device-side name, and a name that
    ignored the target's filename budget would reintroduce exactly the
    ``File name too long`` failure :func:`staged_temp_name` exists to prevent
    -- ``<temp>.uu`` is three characters over the cap for any destination that
    used all of it.
    """

    def __init__(self, max_filename_len: int) -> None:
        self._max_filename_len = max_filename_len

    def _scratch_for(self, temp: Path) -> Path:
        """Name the device-side scratch this codec decodes each chunk into.

        :func:`staged_temp_name` again, applied to the STAGED TEMP's own
        basename: same directory, same filename budget, and a fresh random
        token of its own so the scratch can collide neither with the temp it
        sits beside nor with another transfer's scratch. Truncating the
        prefix rather than the token is that function's discipline and it is
        the right one here too -- nothing reads this name back, it exists to
        be created, catted and removed inside one command.

        ONE PER FILE, not per chunk: ``uudecode -o`` truncates an existing
        target, so every chunk reuses the same path, and the command that
        creates it also removes it.
        """
        return temp.parent / staged_temp_name(temp.name, self._max_filename_len)

    @override
    async def send_chunks(self, loop: PutChunkLoop) -> ChunkLoopOutcome:
        """Frame, decode and append one chunk at a time -- one command each.

        The first chunk redirects with ``>`` and every later one with ``>>``,
        matching :meth:`Base64Codec.send_chunks`: an append would otherwise
        add to a temp that somehow already existed rather than replacing it.

        :attr:`PutChunkLoop.on_sent` is called only after the command
        succeeded, so the running digest and byte count never count a chunk
        the device did not take.

        On failure the scratch is swept a second time before returning. The
        command removes it itself on both of its own paths, so this only
        covers the one it cannot: a command that never ran to completion
        because the transport dropped it. Best-effort and silent, like
        :meth:`ShellFileTransfer._cleanup_temp`, and for the same reason --
        the file has already failed.
        """
        scratch = self._scratch_for(loop.temp)
        quoted_scratch = shlex.quote(str(scratch))
        sent = 0
        with loop.src.open("rb") as f:
            while True:
                chunk = f.read(_SHELL_CHUNK_BYTES)
                if not chunk:
                    break
                redirect = ">>" if sent else ">"
                result = await loop.exec_cmd(
                    f"uudecode -o {quoted_scratch} <<'{_UU_HEREDOC_DELIMITER}' && "
                    f"cat -- {quoted_scratch} {redirect} {loop.quoted_temp}\n"
                    f"{_uu_frame(chunk)}\n"
                    f"{_UU_HEREDOC_DELIMITER}\n"
                    f"otto_rc=$?\n"
                    f"rm -f -- {quoted_scratch}\n"
                    f"(exit $otto_rc)"
                )
                if not result.is_ok:
                    await self._sweep_scratch(loop.exec_cmd, quoted_scratch)
                    return ChunkLoopOutcome(
                        sent,
                        f"writing a chunk to {loop.temp} failed "
                        f"(exit {result.retcode}): {result.value or result.msg}",
                    )
                sent += 1
                loop.on_sent(chunk)
        return ChunkLoopOutcome(sent)

    @staticmethod
    async def _sweep_scratch(
        exec_cmd: "Callable[..., Coroutine[Any, Any, CommandResult]]", quoted_scratch: str
    ) -> None:
        """Best-effort removal of the device-side scratch after a failed chunk."""
        result = await exec_cmd(f"rm -f -- {quoted_scratch}")
        if not result.is_ok:
            _logger.debug(
                f"sweep of uu scratch {quoted_scratch} failed (ignored): "
                f"{result.value or result.msg}"
            )

    @override
    async def fetch_chunks(self, loop: GetChunkLoop) -> ChunkLoopOutcome:
        """Ask ``dd`` for each block range, uuencoded, and unframe it locally.

        One command per chunk and no scratch anywhere: the device writes the
        frame to stdout and every decode is local Python. ``uuencode``
        requires a name argument, which is :data:`_UU_FRAME_NAME` and is
        discarded -- what the device calls the stream has no bearing on where
        the bytes go.
        """
        num_chunks = (loop.total + _SHELL_CHUNK_BYTES - 1) // _SHELL_CHUNK_BYTES
        quoted_if = shlex.quote(f"if={loop.src}")
        for k in range(num_chunks):
            result = await loop.exec_cmd(
                f"dd {quoted_if} bs={_SHELL_CHUNK_BYTES} skip={k} count=1 2>/dev/null "
                f"| uuencode {_UU_FRAME_NAME}"
            )
            if not result.is_ok:
                return ChunkLoopOutcome(
                    k,
                    f"reading chunk {k} failed (exit {result.retcode}): "
                    f"{result.value or result.msg}",
                )
            try:
                decoded = _uu_unframe(result.value or "")
            except ValueError as e:
                return ChunkLoopOutcome(k, f"chunk {k} was not a valid uuencode frame: {e}")
            loop.on_received(decoded)
        return ChunkLoopOutcome(num_chunks)


class ShellFileTransfer(UnixFileTransfer):
    """File transfer using nothing but command execution -- no scp, nc, or rsync.

    Every byte moves as encoded text piped through the device's own shell --
    the last resort for a host with no ``scp``, no ``nc``, and no ``rsync``.

    THIS CLASS IS THE STAGING SKELETON, not the encoding. It names the temp,
    handles the empty-file case, verifies before the rename, renames, and
    cleans up; a ``ShellCodec`` moves the bytes in between. Which one is
    ``_select_codec``'s single decision, taken once per ``_run_put`` /
    ``_run_get`` from the device's own probe: ``Base64Codec`` wherever
    ``base64`` is available, ``UuencodeCodec`` on a device measured not to
    have it.

    PUT chunks the local file into plaintext pieces of at most
    ``_SHELL_CHUNK_BYTES`` (see that constant's note for what shrinks them on a
    line-disciplined transport); each piece is base64-encoded locally and appended to a
    same-directory temp file (``<dest>.otto-<unique>``); once every chunk has
    landed, an integrity check (see ``_verify_integrity``) runs against
    the temp, and only on a match is it moved onto the real destination with
    ``mv``. See ``_put_one`` for why the temp-then-mv shape is
    load-bearing, not decorative.

    GET asks the device for the source file's size, then pulls it back
    ``_SHELL_CHUNK_BYTES`` at a time via ``dd | base64`` (the device only
    encodes; every decode is local Python), staging the decoded bytes in a
    same-directory local temp; the same integrity check then confirms the
    temp against the remote source, and only on a match does
    ``Path.replace()`` swing it onto the real destination. See
    ``_get_one``.

    Two things are hard dependencies, unlike :class:`~otto.host.transfer.nc.NcFileTransfer`
    (whose ``userland`` is optional -- see its own docstring for why):

    ``exec_cmd``
        Every unix backend needs this; there is no other way to run
        anything on the device.
    ``userland``
        WHICH CODEC this backend can use at all, and which flag spells
        "decode", can only be known by asking the device -- see
        :attr:`~otto.host.userland.Userland.base64_flag`,
        :meth:`~otto.host.userland.Userland.has_applet` and
        ``_select_codec``. Assuming a
        GNU-coreutils spelling breaks on every BusyBox row in the matrix
        (``base64 --decode`` is rejected everywhere it was tested; see
        :attr:`~otto.host.options.UserlandOptions.base64_flag`), and
        assuming BusyBox's ``-d`` breaks GNU hosts the other way. GET reads
        the same object for :attr:`~otto.host.userland.Userland.stat_size`
        -- whether the device sizes a file with ``stat`` or falls back to
        ``wc`` can only be known the same way. Both directions also read
        :attr:`~otto.host.userland.Userland.checksum` for
        ``_verify_integrity`` -- whether the device has ``md5sum`` at
        all, gating a checksum comparison against the weaker byte-size one.

    Both are enforced in :meth:`create`, before any bytes move.
    """

    host_families = frozenset({"unix"})

    # Both directions move `_SHELL_CHUNK_BYTES` at a time and report once
    # per chunk: GET's codecs emit `dd bs=_SHELL_CHUNK_BYTES count=1` per chunk
    # and report each decoded one, PUT reads the same size -- or LESS, where
    # `_fitted_chunk_bytes` shrinks it to fit the host's exec line budget. A
    # stride is an UPPER bound on the advance between two events, so the
    # unfitted size is the honest declaration for both arms.
    progress_granularity = ProgressGranularity(put=_SHELL_CHUNK_BYTES, get=_SHELL_CHUNK_BYTES)

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        userland: "Userland",
        max_filename_len: int = 255,
        exec_line_budget: "Callable[[], int | None] | None" = None,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self._userland = userland
        # How long one line of a chunk command may be on this host, ASKED of
        # the session manager rather than derived from `connections.term` here
        # (see `_line_budget`). Optional so a builder that predates the seam --
        # or a test constructing this class directly -- keeps the unbudgeted
        # behaviour, which is also what an `ssh` host gets.
        self._exec_line_budget = exec_line_budget

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ShellFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "ShellFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("ShellFileTransfer requires exec_cmd on the transfer context")
        if ctx.userland is None:
            raise ValueError(
                "ShellFileTransfer requires a userland resolver on the transfer context -- "
                "shell put and get must know whether base64 is even present, get must also "
                "know how to size a file, and both must know whether they can verify a "
                "transfer landed intact (md5sum or a size probe) before either emits a "
                "single command"
            )
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            userland=ctx.userland,
            max_filename_len=ctx.max_filename_len,
            exec_line_budget=ctx.exec_line_budget,
        )

    # ------------------------------------------------------------------
    # Protocol dispatch (implements BaseFileTransfer's abstract methods)
    # ------------------------------------------------------------------

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Sequential shell PUT: pick a codec or refuse, then chunk-and-mv one file at a time.

        Two userland questions are answered before the loop -- and before
        anything else in this method -- because each is about the WHOLE
        transfer, not about any one file: if the device can run neither codec,
        or has no way to confirm a PUT landed intact, every file would fail
        identically, and issuing the first file's chunk command anyway would
        read as "we tried and it failed" when nothing was tried at all. See
        :exc:`~otto.host.errors.UnsupportedOnUserlandError`.

        The CODEC is chosen first, by :meth:`_select_codec`, which also owns
        the refusal for a device that can run neither: without an encoder no
        chunk can be moved at all, so nothing else here can run.
        ``checksum == "absent" and stat_size == "absent"`` together is
        checked second: :meth:`_put_one` always verifies its temp before the
        final ``mv`` (see :meth:`_verify_integrity`), and with neither a
        digest tool nor a size probe on the device there is no way to run
        that check at all -- refusing here, before a single chunk is sent,
        keeps that requirement as loud as the ``base64`` one instead of
        surfacing as a per-file failure partway through the first transfer
        attempted. Unhit by the BusyBox matrix (measured: ``md5sum`` and
        ``stat``/``wc`` are present on every row), so this is a unit-only
        branch, like GET's own ``stat_size == "absent"`` refusal below.

        ``resolve()`` is idempotent, serialized, and rate-limited (see
        :meth:`~otto.host.userland.Userland.resolve`), so calling it once per
        ``_run_put`` -- rather than once per file -- is the intended usage,
        matching :meth:`~otto.host.transfer.nc.NcFileTransfer.prepare`.

        Sequential, like :class:`~otto.host.transfer.ftp.FtpFileTransfer`:
        this backend has no concurrency story of its own (every chunk is one
        more exec round trip on the same control path), so a failure stops
        the batch and every file not yet attempted is marked
        ``Status.Skipped`` rather than guessed at.
        """
        await self._userland.resolve()
        codec = self._select_codec("put", "uudecode")
        checksum = self._userland.checksum
        stat_size = self._userland.stat_size
        if checksum == "absent" and stat_size == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs a way to confirm a PUT landed intact, "
                "but this host's userland resolved checksum='absent' and stat_size='absent' "
                "-- neither md5sum nor a size probe (`stat -c %s` / `wc -c`) answered on "
                "this host. Nothing was attempted; transfer with a backend this host "
                "actually supports."
            )
        per_file: dict[Path, Result] = {}
        for i, src in enumerate(src_files):
            dst = dest_dir / src.name
            handler = progress_factory() if progress_factory is not None else None
            result = await self._put_one(src, dst, codec, checksum, stat_size, handler)
            per_file[src] = result
            if not result.is_ok:
                mark_skipped(per_file, src_files[i + 1 :])
                break
        return per_file

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Sequential shell GET: size it, pick a codec, then decode one file at a time.

        Two userland questions are answered before the loop, for the same
        reason :meth:`_run_put`'s own two are: each is about
        the WHOLE transfer, not about any one file, so answering it
        late would read as "we tried and it failed" when nothing was tried
        at all. GET's two are not the same PAIR as PUT's, though: GET never
        needs the ``checksum``-or-``stat_size`` refusal PUT does, because
        GET's ``checksum == "absent"`` fallback reuses the size it already
        fetched for chunk planning (see :meth:`_verify_integrity`) rather
        than needing a second, independent probe the way PUT's fresh
        query on its temp does -- so GET's ``stat_size == "absent"`` refusal
        below already covers the one case that would otherwise leave GET
        with no way to verify either.

        ``stat_size == "absent"`` is checked first: GET cannot chunk a file
        it cannot measure, and ``bytes_total`` in the progress contract
        needs a real number, not a guess. Measured (this phase's task
        brief, ahead of this task's dispatch): every row of the BusyBox
        matrix resolves ``stat_size`` to ``"stat"``, so no matrix device
        reaches this branch today -- but the value is PROBED, not assumed,
        and a non-BusyBox unix host can answer differently, so the branch
        is real and stays.

        The CODEC is chosen second, by the same :meth:`_select_codec`
        :meth:`_run_put` calls -- but for the applet THIS direction needs.
        The device only ENCODES for GET, so what it must have is
        ``uuencode``, not the ``uudecode`` PUT asks about; the ``base64``
        arm needs the same binary either way, even though no *decode* flag of
        its own is ever emitted remotely (decoding happens locally; see
        :meth:`_get_one`).

        Sequential, like :meth:`_run_put`: this backend has no concurrency
        story of its own, so a failure stops the batch and every file not
        yet attempted is marked ``Status.Skipped``.
        """
        await self._userland.resolve()
        stat_size = self._userland.stat_size
        if stat_size == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs a way to size a remote file before "
                "it can chunk the read, but this host's userland resolved "
                "stat_size='absent' -- neither `stat -c %s` nor `wc -c` answered on this "
                "host. Nothing was attempted; transfer with a backend this host actually "
                "supports."
            )
        codec = self._select_codec("get", "uuencode")
        checksum = self._userland.checksum
        per_file: dict[Path, Result] = {}
        for i, src in enumerate(src_files):
            dst = dest_dir / src.name
            handler = progress_factory() if progress_factory is not None else None
            result = await self._get_one(src, dst, codec, stat_size, checksum, handler)
            per_file[src] = result
            if not result.is_ok:
                mark_skipped(per_file, src_files[i + 1 :])
                break
        return per_file

    def _select_codec(self, direction: str, applet: str) -> ShellCodec:
        """Choose the codec this host can actually run, or refuse before anything is sent.

        Called once per ``_run_put`` / ``_run_get``, after ``resolve()``, and
        it is the ONLY place either direction decides how bytes are encoded.

        BASE64 WINS WHEREVER IT IS AVAILABLE, and the preference is a
        measurement rather than a habit. It costs ONE command per chunk
        against uu's one-command-plus-a-scratch-file, its chunk command is
        shorter on the wire (5535 characters against 5832 for the same
        destination), and it is the path whose emitted lines are pinned
        byte-for-byte by ``TestEmittedCommandLinesArePinned``. uu exists for
        the device that has no choice.

        *direction* is ``"put"`` or ``"get"`` and appears in the refusal;
        *applet* is the one THAT direction needs, and the two are not the
        same. otto encodes locally for PUT and the device only decodes, so PUT
        needs ``uudecode``; GET is the mirror image and needs ``uuencode``.
        They are separate applets in :data:`~otto.host.userland.PROBED_APPLETS`
        and a device could ship one without the other, so neither direction is
        allowed to answer for the other -- passing the wrong one here would be
        a guard that fires on the wrong evidence.

        THREE OUTCOMES, AND ``is_settled`` DECIDES BETWEEN TWO OF THEM.
        ``base64_flag`` reads ``"absent"`` both for a device that answered "no
        base64 here" and for one whose probe round never arrived at all
        (``_UNASKABLE_DEFAULTS``), and those two must not lead to the same
        place:

        * a SETTLED absence -- measured on the device, or declared in the
          host's ``userland_options`` -- is a fact, and otto switches codecs
          on it. That is the 1.16.1 case, and it is also how a test forces the
          uu path onto a device that has both.
        * an UNSETTLED absence is not a fact about the device, so it does not
          get to select a codec. This path refuses instead, exactly as the
          backend did before uu existed, and says which of the two it is --
          the message this replaces claimed "no base64 binary was found
          there" for a probe round that never asked.

        The refusal for a device with NEITHER codec gates on ``is_settled``
        the same way and in the same direction: an applet whose batch was
        discarded is not evidence of absence, so an unsettled ``applet_*``
        DEGRADES to attempting uu rather than refusing. ``base64`` is already
        ruled out by the time that matters, so attempting is strictly better
        than declining -- and if the applet really is missing, the device says
        so on the first chunk with the temp cleaned up behind it.
        """
        flag = self._userland.base64_flag
        if flag != "absent":
            return Base64Codec(flag)
        if not self._userland.is_settled("base64_flag"):
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell {direction} needs a codec on the remote host, and "
                "this host's userland could not be asked which it has -- base64_flag is "
                "sitting at its cannot-ask default ('absent'), which is not a measurement "
                "that the device has no base64. Nothing was attempted; the usual cause is "
                "an sshd refusing further exec channels, so retry, or declare "
                "base64_flag / applet_uudecode in this host's userland_options."
            )
        if (
            self._userland.is_settled(applet_capability(applet))
            and self._userland.has_applet(applet) == APPLET_ABSENT
        ):
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell {direction} needs either base64 or {applet} on the "
                f"remote host, and this host's userland resolved base64_flag='absent' and "
                f"applet_{applet}='absent' -- neither encoder was found there. Nothing was "
                f"attempted; install one of them on the device or transfer with a backend "
                f"this host actually supports."
            )
        return UuencodeCodec(self._max_filename_len)

    def _line_budget(self) -> int | None:
        """Characters this host's exec path carries on ONE command line, or ``None``.

        ASKED, NEVER PREDICTED. The obvious cheap answer is in reach from here
        -- ``self._connections.term`` is right there -- and it is wrong twice
        over: a PROXIED login routes ``exec`` through the pooled shell on an
        ``ssh`` host too, and the framing this number is net of belongs to the
        host's command frame, which no transfer backend can see. So the
        question goes to the object that decides both
        (:attr:`~otto.host.session.SessionManager.exec_line_budget`, reached
        through the context this backend was built with) and a
        ``None`` callable -- an embedded builder, a direct construction in a
        test -- means the same thing an unbudgeted route means.

        Read per file rather than cached per transfer, because it is free and
        because the two halves of the arithmetic belong together: the other
        half is the staged temp's path, which :meth:`_put_one` names one file
        at a time.
        """
        return self._exec_line_budget() if self._exec_line_budget is not None else None

    # ------------------------------------------------------------------
    # Shell put
    # ------------------------------------------------------------------

    async def _put_one(
        self,
        src: Path,
        dst: Path,
        codec: ShellCodec,
        checksum: str,
        stat_size: str,
        handler: "TransferProgressHandler | None",
    ) -> Result:
        """PUT one file: stage a temp, hand the chunk loop to *codec*, verify, mv.

        Stages as ``<dst>.otto-<unique>`` in ``dst``'s own directory (named
        by :func:`staged_temp_name`, which keeps that whole basename inside
        the ``max_filename_len`` :meth:`~otto.host.transfer.base.BaseFileTransfer.put_files`
        already validated *src*'s own name against) --
        deliberately not under ``/tmp``, which is not guaranteed to be the
        same filesystem as the destination, and a cross-filesystem ``mv``
        degrades to a copy, losing the atomicity a same-filesystem rename
        gives the final step.

        FILLING that temp is *codec*'s job, not this method's: everything
        between naming it and verifying it is one
        :meth:`ShellCodec.send_chunks` call, and how many commands that costs,
        what they say, and in what order encoded and plaintext bytes reach the
        temp are the codec's to choose (see :class:`ShellCodec` for the
        measurement that made the whole loop the unit rather than an
        ``encode()`` call, and :meth:`Base64Codec.send_chunks` for what the
        ``base64`` one does). This method contributes exactly four things to
        the bytes: the temp's name, the empty-file case, the running state the
        codec reports into, and the transport's line budget (see
        :meth:`_line_budget` and :attr:`PutChunkLoop.line_budget`) -- which is
        a fact ABOUT the transport, handed over for a codec to spend or ignore,
        not a chunk size chosen here.

        THE EMPTY-FILE CASE IS SHARED, and stays here because it is not an
        encoding at all -- a source with no bytes yields no chunks from any
        codec, and ``: > <temp>`` creates the temp without one.
        :attr:`ChunkLoopOutcome.chunks` is how this method learns that
        happened.

        The temp and destination paths are quoted with ``shlex.quote`` plus a
        ``--`` terminator on every command that takes a path as a positional
        argument (``mv``, ``rm``), matching
        :func:`~otto.host.transfer.base.chmod_command`'s precedent, because
        ``shlex.quote`` alone leaves a leading-dash basename like ``-rf``
        unquoted and a command without ``--`` would then read it as flags. The
        codec is handed the already-quoted form
        (:attr:`PutChunkLoop.quoted_temp`) so it cannot get that wrong either.

        Any failure -- a chunk write, the temp's creation, a failed or
        mismatched integrity check, or the final ``mv`` -- removes the temp
        (best-effort; its own failure is logged, not raised, since the file
        to report on has already failed) and returns a failing
        :class:`~otto.result.Result`. A local read error (the source
        vanished, permissions changed) is caught the same way, since by the
        time this runs :meth:`BaseFileTransfer.put_files` has already
        validated the file list -- this is a race, not a precondition otto
        skipped.

        AN INTERRUPT IS NOT A FAILURE PATH and does not reach any of those
        calls: ``asyncio.CancelledError`` is a ``BaseException``, so a
        cancelled chunk loop walks straight past ``except OSError`` and out
        of this method with no cleanup reached at all. It gets its own
        handler, and the awaited removal there needs shielding and a bound
        that a plain error path does not -- see
        :meth:`_cleanup_temp_interrupted`. What is NOT at risk either way is
        the destination: the temp-then-``mv`` discipline above means an
        interrupt can only ever leave an inert staged temp, never a short
        file at the real path (measured on the bed, and asserted by
        ``tests/e2e/chaos/test_transfer_chaos.py``'s BusyBox arm).

        Verification (see :meth:`_verify_integrity`) runs on the TEMP, after
        every chunk has landed and before the ``mv`` -- a mismatch is
        therefore caught before it ever reaches the real destination, not
        reported after the fact against a file the caller may already be
        reading. Every chunk-write byte also updates a running
        :func:`hashlib.md5` locally, so an ``md5sum`` verification costs no
        second local read pass over *src*.
        """
        temp = dst.parent / staged_temp_name(dst.name, self._max_filename_len)
        quoted_temp = shlex.quote(str(temp))
        try:
            total = src.stat().st_size
            bytes_done = 0
            # MD5 is a corruption check against `md5sum`, the tool name this
            # capability is named for and the only one BusyBox ships -- never
            # a security boundary, so the collision-resistance ruff's S324
            # warns about does not apply here.
            local_digest = hashlib.md5()  # noqa: S324

            def _sent(chunk: bytes) -> None:
                nonlocal bytes_done
                bytes_done += len(chunk)
                local_digest.update(chunk)
                if handler is not None:
                    handler(str(src), str(dst), bytes_done, total)

            outcome = await codec.send_chunks(
                PutChunkLoop(
                    exec_cmd=self._exec_cmd,
                    src=src,
                    temp=temp,
                    quoted_temp=quoted_temp,
                    on_sent=_sent,
                    line_budget=self._line_budget(),
                )
            )
            if outcome.error is not None:
                await self._cleanup_temp(quoted_temp)
                return Result(Status.Error, msg=f"{src}: {outcome.error}")

            if outcome.chunks == 0:
                create_result = await self._exec_cmd(f": > {quoted_temp}")
                if not create_result.is_ok:
                    await self._cleanup_temp(quoted_temp)
                    return Result(
                        Status.Error,
                        msg=(
                            f"{src}: creating empty temp {temp} failed "
                            f"(exit {create_result.retcode}): "
                            f"{create_result.value or create_result.msg}"
                        ),
                    )
                if handler is not None:
                    handler(str(src), str(dst), 0, 0)

            mismatch = await self._verify_integrity(
                temp,
                checksum=checksum,
                stat_size=stat_size,
                local_digest=local_digest,
                local_size=bytes_done,
            )
            if mismatch is not None:
                await self._cleanup_temp(quoted_temp)
                return Result(Status.Error, msg=f"{src}: integrity check failed -- {mismatch}")

            mv_result = await self._exec_cmd(f"mv -- {quoted_temp} {shlex.quote(str(dst))}")
            if not mv_result.is_ok:
                await self._cleanup_temp(quoted_temp)
                return Result(
                    Status.Error,
                    msg=(
                        f"{src}: moving {temp} to {dst} failed "
                        f"(exit {mv_result.retcode}): {mv_result.value or mv_result.msg}"
                    ),
                )
        except asyncio.CancelledError:
            await self._cleanup_temp_interrupted(quoted_temp)
            raise
        except OSError as e:
            await self._cleanup_temp(quoted_temp)
            return Result(Status.Error, msg=f"{src}: could not read local file: {e}")
        return Result(Status.Success, value=dst)

    async def _cleanup_temp(self, quoted_temp: str) -> None:
        """Best-effort removal of a staged temp after a failed chunk/create/mv step.

        Fire-and-forget on purpose: the file this belongs to has already
        failed, and a caller reading a two-part failure ("chunk write
        failed" AND "cleanup also failed") learns nothing a debug log
        doesn't already cover. ``rm -f`` so a temp that was never created
        (the very first chunk failed) is not itself an error.
        """
        result = await self._exec_cmd(f"rm -f -- {quoted_temp}")
        if not result.is_ok:
            _logger.debug(
                f"{self._name}: cleanup of {quoted_temp} failed (ignored): "
                f"{result.value or result.msg}"
            )

    async def _cleanup_temp_interrupted(self, quoted_temp: str) -> None:
        """Remove the staged temp on the way out of a CANCELLED put: shielded and bounded.

        The same best-effort ``rm`` :meth:`_cleanup_temp` issues, run in the
        one place a plain ``await`` cannot be trusted to finish. The gap was
        MEASURED, not reasoned about: the BusyBox arm of
        ``tests/e2e/chaos/test_transfer_chaos.py`` SIGINTs a shell PUT
        mid-chunk-loop and found the ``<dest>.otto-<token>`` temp still on the
        guest afterwards -- an inert file, one per interrupted transfer, since
        ``except OSError`` never sees a ``CancelledError``.

        THIS METHOD NEVER SWALLOWS A CANCELLATION. Its caller re-raises the
        one it caught; a cancellation arriving DURING the cleanup propagates
        out of here in its place. Neither is ever converted into an ordinary
        exception -- an operator who pressed Ctrl+C must get the interrupt's
        exit path, not a transfer that looks like it failed. The one thing
        that DOES outrank a cancellation here is another ``BaseException``:
        ``KeyboardInterrupt`` and ``SystemExit`` are deliberately outside the
        ``except Exception`` below, because an interpreter that is exiting
        outranks a temp file.

        THREE CASES, AND THE CONSTRUCT HAS TO ANSWER ALL THREE.

        ONE CANCEL (the ordinary Ctrl+C). The ``CancelledError`` has already
        been delivered by the time the handler runs and asyncio does not
        redeliver it, so awaiting here completes normally. This case alone
        would need no shield -- only the bound below, because the ``rm``
        inherits a 30 s command timeout that outlasts the teardown deadline.

        A SECOND CANCEL DURING THE CLEANUP (a second Ctrl+C, or the run's
        force deadline firing). That one IS delivered, and it lands on this
        method's own await -- tearing the ``rm`` mid-flight and stranding
        exactly the file this method exists to remove. ``asyncio.shield``
        alone does not fix it: shield keeps the cancellation off the inner
        task, but the AWAITING side still raises at once, so the ``rm`` would
        be left running detached -- a leaked temp traded for a stray task,
        which this repo's coverage lanes arm a detector for.
        :func:`otto.lifecycle.compensate` is the shape that does hold: it
        keeps the shielded task running, HOLDS the second cancellation, and
        re-raises it once the cleanup resolves or the bound below expires
        (cancelling and joining the inner task first, so nothing outlives
        this call either way). Same helper the nc backend's listener reap
        uses for the same reason, with the same bound turned on.

        THE SESSION ALREADY GONE, so the ``rm`` raises instead of returning a
        failing :class:`~otto.result.CommandResult` -- a transport torn down
        under a dying process is the likeliest way this method is ever
        reached. Caught here, at ``Exception`` and never ``BaseException``,
        and logged at debug: a raise would replace the caller's cancellation
        with an ``OSError``, and there is nothing to report anyway, since the
        file this belongs to is already interrupted. ``BaseException`` is
        excluded on purpose -- that is how compensate's held cancellation
        gets out.

        THE BOUND (:data:`_INTERRUPTED_CLEANUP_TIMEOUT`) IS COMPENSATE'S OWN
        ``timeout=``, not a wrapper around the ``rm``: the helper arms it at
        the call, so it is the same 2 s in every case above -- whether or not
        a second interrupt ever arrives -- and on expiry the helper cancels
        the shielded ``rm``, joins it, and returns. No ``deadline=`` rides
        along with it, deliberately: that bound is armed by the first HELD
        cancellation, so an equal one could only ever expire LATER than this
        one, and the default it resolves to (``OTTO_TEARDOWN_DEADLINE``) is
        the very window this bound exists to fit inside.

        Expiry is not a raise: the cleanup is abandoned with a WARNING naming
        the action -- a temp this method promised to remove may still be on
        the device, which is not something to hide in a debug line -- the
        caller's cancellation still propagates, and teardown keeps its
        deadline.
        """
        # Imported here, not at module scope: otto.lifecycle is only needed
        # once a compensating action actually runs, and a top-level import
        # drags it onto every CLI --help path (import-budget guard). Mirrors
        # `otto.host.transfer.nc`'s own compensate import.
        from ...lifecycle import compensate

        async def _suppressed_cleanup() -> None:
            # The suppression is CALLER POLICY and stays out here rather than
            # moving into compensate(): the helper re-raises an inner failure
            # when no cancellation is held, so an `rm` that raises on a torn
            # transport would reach `_put_one` IN PLACE OF the CancelledError
            # it is unwinding. Nothing about a bound changes that.
            try:
                await self._cleanup_temp(quoted_temp)
            except Exception as e:  # noqa: BLE001 — a dying transport must not replace the interrupt
                _logger.debug(
                    f"{self._name}: cleanup of {quoted_temp} failed during an interrupted "
                    f"put (ignored): {e}"
                )

        await compensate(
            _suppressed_cleanup(),
            timeout=_INTERRUPTED_CLEANUP_TIMEOUT,
            what=f"{self._name}: staged temp removal after an interrupted put",
        )

    # ------------------------------------------------------------------
    # Shell get
    # ------------------------------------------------------------------

    async def _get_one(
        self,
        src: Path,
        dst: Path,
        codec: ShellCodec,
        stat_size: str,
        checksum: str,
        handler: "TransferProgressHandler | None",
    ) -> Result:
        """GET one file: size it, hand the chunk loop to *codec*, verify, replace into place.

        PUT's temp-then-mv shape, mirrored onto the local side rather than
        the remote one: every chunk lands in a same-directory local temp
        (``<dst>.otto-<unique>``, named by the same
        :func:`staged_temp_name` PUT uses, so the same ``max_filename_len``
        budget applies to a name
        :meth:`~otto.host.transfer.base.BaseFileTransfer.get_files` already
        validated), and ``Path.replace()`` swings it onto
        *dst* only once every chunk has arrived. Staying in ``dst``'s own
        directory is not merely the same discipline PUT follows -- for GET
        it is load-bearing in a way PUT's staging choice is not: PUT's
        remote ``mv`` can, on some implementations, degrade to a copy
        across filesystems and still succeed; ``Path.replace()`` has
        no such fallback and raises outright
        (``OSError: [Errno 18] Invalid cross-device link``, measured
        locally) when source and destination are on different filesystems.

        Sizing runs first, via *stat_size* (``"stat"`` or ``"wc"`` --
        ``"absent"`` is refused earlier, in :meth:`_run_get`, before this
        method is ever called), and the number it returns is handed to
        *codec* as :attr:`GetChunkLoop.total`. PULLING the bytes is then one
        :meth:`ShellCodec.fetch_chunks` call: how many chunks that is, what
        each one's command says, and how its text decodes are the codec's
        (see :meth:`Base64Codec.fetch_chunks` for the ``base64`` answers, and
        :class:`ShellCodec` for why the unit is the loop). What stays here is
        the local staging temp -- this method opens it, and
        :attr:`GetChunkLoop.on_received` is the only way the codec can reach
        it, so a codec never touches the local filesystem.

        The device only ENCODES for GET, whichever codec is in use: every
        decode runs locally, in Python, so no decode flag is emitted remotely
        the way PUT emits one. A remote file of zero bytes yields zero chunks,
        and the local temp is then created empty by opening it for writing --
        no special-cased remote command is needed the way PUT's empty-file
        branch needs one, because GET's "local file" really is local.
        :attr:`ChunkLoopOutcome.chunks` is how this method learns that
        happened and fires its one ``(0, 0)`` progress call.

        The size probe and every chunk read are separate exec round trips
        against the same remote path -- *src* can grow or shrink on the
        device in between them (a TOCTOU PUT's own local ``stat()``-then-read
        shares in miniature), and neither this method nor any codec detects
        or guards against it; a shrink mid-transfer surfaces as one chunk
        reading short, silently under-filling that chunk, not as an error.

        Any failure -- a chunk read, decoding a chunk's text, a failed or
        mismatched integrity check, or a local write -- removes the local
        temp (best-effort; see :meth:`_cleanup_local_temp`) and returns a
        failing :class:`~otto.result.Result`. The size probe is the one
        exception: its failure (``_remote_size`` returning ``None``) returns
        before the chunk loop or the ``temp.open()`` call ever runs, so
        there is no local temp yet to remove -- see the ``total is None``
        branch below. An INTERRUPT has the same hole PUT's did and gets the
        same handler for the same reason (``CancelledError`` is a
        ``BaseException``, so ``except OSError`` never sees it, and a
        cancelled chunk loop would leave ``<dst>.otto-<token>`` sitting in
        the destination's directory) -- but none of PUT's machinery, because
        removing this temp is a local ``unlink`` and not an await: there is
        no round trip to shield from a second cancellation and nothing to
        bound. See :meth:`_cleanup_local_temp`. What the interrupt path DOES
        need here is the temp file's own ``close()`` kept off it -- see the
        comment on the ``temp.open("wb")`` below, since a flush failure
        raised while unwinding would replace the cancellation with an
        ``OSError`` and report an interrupted GET as a failed one.

        A source path is a REMOTE path here (unlike PUT's *src*, which is
        local), so there is nothing local to race against before the loop
        starts -- only ``dst``'s directory needs to exist, and if it does
        not, the ``OSError`` from opening the local temp is caught the same
        way every other local write failure is.

        Verification (see :meth:`_verify_integrity`) runs on the local temp,
        after every chunk has been decoded and written and before
        ``Path.replace()`` -- a mismatch never reaches *dst*. On the
        ``checksum == "md5sum"`` path this is a fresh ``md5sum`` round trip;
        on the ``checksum == "absent"`` path it reuses *total*, the size
        this method already fetched to plan the chunk loop, rather than
        re-querying the device -- see :meth:`_verify_integrity` for why that
        does not weaken what the check catches. Every decoded chunk also
        updates a running :func:`hashlib.md5` locally, so an ``md5sum``
        verification costs no second local read pass over the temp.
        """
        temp = dst.parent / staged_temp_name(dst.name, self._max_filename_len)
        try:
            total = await self._remote_size(src, stat_size)
            if total is None:
                return Result(
                    Status.Error,
                    msg=f"{src}: could not determine the remote file's size (stat/wc probe "
                    "failed or returned unparseable output)",
                )
            bytes_done = 0
            # MD5 is a corruption check against `md5sum`, the tool name this
            # capability is named for and the only one BusyBox ships -- never
            # a security boundary, so the collision-resistance ruff's S324
            # warns about does not apply here.
            local_digest = hashlib.md5()  # noqa: S324
            # NOT `with temp.open(...)`, and the difference is a cancellation:
            # closing a buffered file FLUSHES it, so `close()` can raise
            # (ENOSPC, EIO) -- and a `with` block raises that from its
            # `__exit__` while an exception is already in flight, REPLACING
            # it. On the cancel-unwind path the replacement is an `OSError`,
            # which this method's own `except OSError` then turns into a
            # failed :class:`~otto.result.Result` -- an interrupted GET
            # reported as a failed one, the exact defect the PUT path's
            # interrupt handling exists to avoid. So the normal close stays
            # inside the try (where a flush failure IS the transfer's error,
            # as it has always been) and the unwind close is best-effort.
            f = temp.open("wb")
            try:

                def _received(decoded: bytes) -> None:
                    nonlocal bytes_done
                    f.write(decoded)
                    bytes_done += len(decoded)
                    local_digest.update(decoded)
                    if handler is not None:
                        handler(str(src), str(dst), bytes_done, total)

                outcome = await codec.fetch_chunks(
                    GetChunkLoop(
                        exec_cmd=self._exec_cmd,
                        src=src,
                        total=total,
                        on_received=_received,
                    )
                )
                f.close()
            finally:
                # Idempotent: on the normal path the close above already ran
                # and this one is a no-op (a `close()` that raises still
                # closes the file), so this only ever fires while unwinding.
                with contextlib.suppress(OSError):
                    f.close()
            if outcome.error is not None:
                self._cleanup_local_temp(temp)
                return Result(Status.Error, msg=f"{src}: {outcome.error}")
            if outcome.chunks == 0 and handler is not None:
                handler(str(src), str(dst), 0, 0)

            mismatch = await self._verify_integrity(
                src,
                checksum=checksum,
                stat_size=stat_size,
                local_digest=local_digest,
                local_size=bytes_done,
                known_remote_size=total,
            )
            if mismatch is not None:
                self._cleanup_local_temp(temp)
                return Result(Status.Error, msg=f"{src}: integrity check failed -- {mismatch}")

            temp.replace(dst)
        except asyncio.CancelledError:
            self._cleanup_local_temp(temp)
            raise
        except OSError as e:
            self._cleanup_local_temp(temp)
            return Result(Status.Error, msg=f"{src}: local write failed: {e}")
        return Result(Status.Success, value=dst)

    async def _verify_integrity(
        self,
        remote_path: Path,
        *,
        checksum: str,
        stat_size: str,
        local_digest: "hashlib._Hash",
        local_size: int,
        known_remote_size: int | None = None,
    ) -> str | None:
        """Confirm *remote_path* matches what was sent (PUT) or received (GET).

        Returns ``None`` on a match, or a human-readable reason on a
        mismatch or a failed verification command -- the caller turns
        either into a failing :class:`~otto.result.Result` and removes its
        temp; this method never does either itself, so it has exactly one
        job: decide match or no match, never format the final message.

        ``checksum == "md5sum"``: a device-side ``md5sum -- <remote_path>``,
        compared against *local_digest* (already accumulated chunk-by-chunk
        by the caller, so no second local read pass). The comparison
        lower-cases both sides before comparing -- every ``md5sum`` measured
        on this matrix emits lowercase hex and so does
        :meth:`hashlib.md5.hexdigest`, so this never matters on a real
        device, but a parser that silently depended on that agreement would
        be a latent bug in waiting, not a verified property. Always a FRESH
        command, for both callers -- there is no cached digest to reuse.

        ``checksum == "absent"``: a byte-size comparison against
        *local_size*, using the exact ``stat``/``wc`` spelling
        :attr:`~otto.host.userland.Userland.stat_size` already resolved.
        *known_remote_size*, when given, is used directly instead of a fresh
        :meth:`_remote_size` round trip -- GET's caller passes the total it
        already fetched to plan the chunk loop, because the remote PATH
        (*src*) is the same one that number came from and nothing here
        changes it; PUT has no such number for its temp (nothing sized it
        before this call) and always issues a fresh query. Reusing rather
        than re-querying does not weaken what this check catches: it still
        compares the ANNOUNCED size against what actually got decoded and
        written, which is exactly the "chunk read short, no error raised"
        gap this task closes -- it only forgoes catching a size change that
        happens strictly between the initial probe and this call. That gap
        is real and is narrower than it looks next to the ``md5sum`` arm
        above: that arm issues its device command fresh on every call, with
        no cached digest ever reused, so a change to the remote file
        partway through a GET is still caught there, even on GET. Only
        THIS arm's reuse of *known_remote_size* gives up that freshness,
        and only for GET -- PUT always queries fresh regardless of which
        arm runs, since PUT has no earlier size or digest to reuse in the
        first place. Weaker than a checksum regardless (two files of the
        same size can still differ), and the caller's message says so
        rather than reporting it identically to a hash match.
        """
        quoted = shlex.quote(str(remote_path))
        if checksum == "md5sum":
            result = await self._exec_cmd(f"md5sum -- {quoted}")
            if not result.is_ok:
                return (
                    f"md5sum verification command failed (exit {result.retcode}): "
                    f"{result.value or result.msg}"
                )
            fields = (result.value or "").split()
            remote_digest = fields[0] if fields else ""
            local_hex = local_digest.hexdigest()
            if remote_digest.lower() != local_hex.lower():
                return (
                    f"md5sum mismatch: device reports {remote_digest!r}, "
                    f"local hash is {local_hex!r}"
                )
            return None
        if known_remote_size is not None:
            remote_size = known_remote_size
            # Reused, not freshly asked -- see this method's own docstring for
            # why that does not weaken the check. The message below must say
            # so: this number is what the device reported BEFORE the
            # transfer, not a report happening now, and "device reports"
            # unqualified would misstate which.
            remote_size_desc = "the size fetched before the transfer was"
        else:
            remote_size = await self._remote_size(remote_path, stat_size)
            if remote_size is None:
                return (
                    "could not verify by size (stat/wc probe failed or returned "
                    "unparseable output; this host has no md5sum, so size is the "
                    "only check available)"
                )
            remote_size_desc = "device reports"
        if remote_size != local_size:
            return (
                f"size mismatch (byte-size comparison only -- this host has no "
                f"md5sum): {remote_size_desc} {remote_size} bytes, local is "
                f"{local_size} bytes"
            )
        return None

    async def _remote_size(self, src: Path, stat_size: str) -> int | None:
        """Query *src*'s byte size the way *stat_size* says this host answers it.

        ``"stat"`` -> ``stat -c %s -- <src>``: a plain positional argument,
        so a leading-dash basename needs the ``--`` terminator (matching
        :func:`~otto.host.transfer.base.chmod_command`'s precedent -- verified
        locally that BusyBox 1.36.1's ``stat`` accepts it identically to
        GNU coreutils'). ``"wc"`` -> ``wc -c < <src>``: a shell redirection
        TARGET, which the shell itself resolves rather than passing to
        ``wc`` as an argument, so no ``--`` is needed there.

        Returns ``None``, never raises, on a non-zero exit or unparseable
        output -- the caller turns that into a failing
        :class:`~otto.result.Result` for the one file rather than an
        exception that would abort files already queued behind it.

        Two callers, and both reach this method -- GET's unconditionally,
        PUT's only when ``checksum`` resolves ``"absent"``.
        GET's own chunk-planning sizing (*src* is the remote source, before
        a single chunk has been requested) always calls this directly.
        :meth:`_verify_integrity`'s ``checksum == "absent"`` path calls this
        too, but only when its caller omits *known_remote_size* -- PUT's
        temp has no earlier size to reuse, so PUT's call always lands here;
        GET's call always supplies the *total* it already fetched for chunk
        planning, so GET never reaches this method a second time (see
        :meth:`_verify_integrity`'s docstring for why reusing rather than
        re-querying does not weaken what GET's absent-checksum check
        catches). The name predates that second caller and stays generic on
        purpose -- despite the parameter name, nothing here is GET-specific.
        """
        quoted_src = shlex.quote(str(src))
        cmd = f"stat -c %s -- {quoted_src}" if stat_size == "stat" else f"wc -c < {quoted_src}"
        result = await self._exec_cmd(cmd)
        if not result.is_ok:
            return None
        try:
            return int((result.value or "").strip())
        except ValueError:
            return None

    def _cleanup_local_temp(self, temp: Path) -> None:
        """Best-effort removal of a staged LOCAL temp after a failed size/read/decode/write step.

        The GET-side analogue of :meth:`_cleanup_temp`: fire-and-forget, for
        the same reason -- the file this belongs to has already failed, and
        there is nothing more informative to report by raising a second
        time. Local, not remote, so there is no exec round trip:
        ``Path.unlink(missing_ok=True)`` covers "never created" the way
        PUT's ``rm -f`` does, without an extra command against the device.

        BEING LOCAL IS ALSO WHY THE INTERRUPT PATH REUSES THIS UNCHANGED,
        where PUT needed :meth:`_cleanup_temp_interrupted` built around its
        remote one. This method does not await, so a cancellation cannot
        land inside it: there is no window for a second Ctrl+C to tear, no
        detached task to strand, and nothing for a timeout to bound. It
        cannot swallow a cancellation either -- the ``except`` below is
        ``OSError``, and ``CancelledError`` is not one.
        """
        try:
            temp.unlink(missing_ok=True)
        except OSError as e:
            _logger.debug(f"{self._name}: cleanup of local temp {temp} failed (ignored): {e}")


register_transfer_backend("shell", ShellFileTransfer)
