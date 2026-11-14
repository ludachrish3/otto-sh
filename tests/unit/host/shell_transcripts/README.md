# Recorded shell transcripts

What each bed shell sent back for a framed command, recorded on 2026-11-13 by
`make shell-transcripts` (`scripts/capture_shell_transcripts.py`, under the bed
lease). One file per `(element, term)`; `test1-ssh-pooled.json` is `test1` over
ssh through the hook-built pooled shell (`host.exec`), every other file is
`host.run` on the cell's default shell session.

Each command entry carries:

- `raw`: every chunk the session's read loop read for the command, joined, as
  read off the transport (before any fold);
- `buffer`: the string the frame's `parse_output` was handed (for a session
  whose `terminal_mapping` is `onlcr`, `raw` with `\r\n` folded to `\n`;
  otherwise identical to `raw`);
- `value` / `retcode`: what otto returned on 2026-11-13.

The model below is checked against every entry: for each file, building the
transcript from the model with the command's output reproduces `raw` byte for
byte (65 Zephyr entries, 36 POSIX entries).

Notation: `P` is the prompt, `RED` is `\x1b[1;31m`, `YEL` is `\x1b[1;33m`, `RST` is `\x1b[m`, `B` and
`E` are `markers.begin` and `markers.end_prefix`. `out` is the command's output
as the shell sent it.

## Zephyr 3.7 / 4.4: `zephyr` (telnet) and `zephyr-serial` (console)

Cells: `zephyr37_fat`, `zephyr37_lfs`, `zephyr44_lfs` (telnet, `zephyr`);
`zephyr37_nofs`, `zephyr37_llext`, `zephyr44_llext` (console, `zephyr-serial`).
`terminal_mapping` is `unmeasured` (the frame does not measure it): no fold.

The frame types `B\r<cmd>\rretval\rE\r`. The shell does not echo input. The
read stops at `E`, so the tail of the previous command's END line is the head
of the next buffer.

```
buffer = ": command not found\r\n" RST P "\r\n"        # tail of the previous E line, its prompt, the CR that runs B
       + RED B ": command not found\r\n" RST P "\r\n"  # B rejected, prompt, the CR that runs <cmd>
       + out                                           # the command
       + P "\r\n"                                      # prompt, the CR that runs retval
       + str(retcode) "\r\n"                           # retval
       + P "\r\n"                                      # prompt, the CR that runs E
       + RED E                                         # read stops here
```

- **Prompt.** `P` is `\x1b[1;32m~$ \x1b[m` on telnet and `\x1b[1;32muart:~$ \x1b[m`
  on the serial console. It is coloured, it does not end with a newline, and it
  is always followed by `\r\n`: the shell's line feed for the CR otto typed.
  Every prompt starts a line, because every `out` recorded ends with `\r\n`.
- **Line ending.** Each line of `out` ends `\r\n`, e.g. `kernel version` →
  `"Zephyr version 3.7.2\r\n"`; blank lines arrive as `\r\n\r\n`
  (`help`: `"...more information.\r\n\r\nShell supports..."`).
- **Message-level colouring.** Each line the shell's message-level API
  writes (`shell_error`, `shell_warn`) is wrapped ON ITS OWN as
  `RED <text>\r\n RST` (error) or `YEL <text>\r\n RST` (warning): the reset
  comes after that line's `\r\n`, and wrapped lines fall anywhere in `out`,
  before or between plain lines. `otto_not_a_command` →
  `"\x1b[1;31motto_not_a_command: command not found\r\n\x1b[m"`, the same
  wrapper the rejected markers get. The commands that show the other shapes
  are `ZEPHYR_EXTRA_COMMANDS` (`tests/_fixtures/shell_transcripts.py`), each
  asked of the guests whose `help` lists it:
  - two error lines, each wrapped separately: `fs cat /nonexistent
    /nonexistent2` →
    `"RED Failed to obtain file /nonexistent (err: -2)\r\n RST RED Failed to obtain file /nonexistent2 (err: -2)\r\n RST"`
    (2.7, 3.7 fat/lfs, 4.4 lfs); `log enable inf otto_nope1 otto_nope2`
    likewise (every build with `log`);
  - an error line, then plain output: `devmem` →
    `"RED devmem: wrong parameter count\r\n RST devmem - Read/write physical memory\r\n\x1b[9CUsage:\r\n..."`
    (every 3.7 and 4.4 build; 2.7 sends nothing, exit `-22`);
  - a warning line: `dynamic show` →
    `"YEL Please add some commands first.\r\n RST"` (builds with `dynamic`);
  - a warning line, then an error line: `kernel log_level otto_nope 1` on 4.4 →
    `"YEL This command is deprecated ...\r\n RST RED Unable to find log source: otto_nope\r\n RST"`.

  No stock command on these builds writes plain output and THEN a coloured
  line, and none writes an info line (`shell_info`, whose colour was not
  measured): those shapes are not recorded. `llext call_fn` of an absent
  extension prints `No such extension <name>\r\n` uncoloured (3.7, 4.4
  llext).
- **ANSI inside output.** `help` (the shell's own help formatter) sends cursor-forward sequences at the start of
  continuation lines: `"\x1b[17CUsage:\r\n"` (telnet), `\x1b[13C` / `\x1b[24C`
  on other builds (the column depends on the longest command name). These sit
  inside `out`, nowhere near a prompt.
- **Exit code** is `retval`'s line, a bare signed integer: `0`, and `-8` for the
  unknown command.

Identical across 3.7 and 4.4 for both frames; the only byte that differs
between `zephyr` and `zephyr-serial` is the prompt text. (4.4's `device list`
adds `  DT node labels: ...` lines and 4.4's `help` lists `abcdefklnptuw`; that
is command output, not frame.)

## Zephyr 2.7: `zephyr-inline` (telnet, test frame)

Cell: `zephyr27_fat`. `terminal_mapping` `unmeasured`. The patched firmware
prints `retCode = <n>\r\n` after every executed line, before the prompt. The
frame types `B\r<cmd>\rE\r` (no `retval`).

```
buffer = ": command not found\r\n" RST "retCode = -8\r\n" P "\r\n"
       + RED B ": command not found\r\n" RST "retCode = -8\r\n" P "\r\n"
       + out
       + "retCode = " str(retcode) "\r\n" P "\r\n"
       + RED E
```

`P` is `\x1b[1;32m~$ \x1b[m`. Line endings, the message-level colouring
(`"\x1b[1;31motto_not_a_command: command not found\r\n\x1b[mretCode = -8\r\n"`)
and in-output ANSI (`"\x1b[13Creadout fails..."` in `help`) are as on 3.7.

## POSIX: `bash` frame (test1 ssh run, test1 ssh pooled exec, bb1161 telnet)

`terminal_mapping` is `onlcr` on all three. Note: the BusyBox cell
(`bb1161`, BusyBox 1.16.1) runs `BashFrame` — its lab entry declares no
`command_frame`, so the `ash` frame was **not** exercised by this capture.

The frame types `echo "B"; { <cmd>\n}; echo "E$?__"\n` with echo off.

```
raw = "\r\n"                       # newline after the previous command's END (absent on the first)
    + PS1 "> "                     # prompt, then the continuation prompt for the brace group
    + B "\r\n"
    + out                          # program bytes, each \n sent as \r\n
    + E str(retcode) "__"          # directly after out: no newline is inserted
```

`PS1` is `vagrant@test1:~$ ` (test1) or `/root # ` (bb1161). No ANSI anywhere.
`printf 'abc'` → `"...BEGIN__\r\nabc__OTTO_..._END__0__"`: output with no
trailing newline is recoverable exactly. Run and pooled exec send identical
bytes apart from the marker id.

## What the frames remove, and what they keep

The exact rule: `value` is the output with exactly one trailing `\n` removed,
where output is `out` with the shell's additions removed. Every recorded
`value` equals that rule (`tests/unit/host/test_shell_transcripts.py`).

| Frame / cell | Bytes | Class |
|---|---|---|
| all Zephyr | `\r` of each `\r\n`, every line of every command | shell added; removed (see caveat 1) |
| all Zephyr | prompt `P` + `\r\n`, `retval` / `retCode` lines, positional around `out` | shell added; removed |
| all Zephyr | `RED`, `YEL` and `RST` of every message-level line, wherever it falls | shell added; removed (see caveat 3) |
| all Zephyr | `\x1b[<n>C` at the start of `help` continuation lines | the shell's help formatter (width varies by build); that command's output; kept |
| all Zephyr | blank lines and leading spaces (`help`) | the command wrote them; kept |
| bb1161 (`bash`) | `printf 'a\tb\n'` → `"a\tb\r\n"` | the tab arrives as written, because the bash handshake turns the tty's tab expansion off (`stty tab0`; `otto.host.terminal_mapping`'s docstring and `docs/architecture/utilities/results.md` give the measurement) |
| test1 run / pooled | none: all twelve values equal the exact rule | |

History: before the frames were changed to this rule, a Zephyr `value` dropped
every escape (the `help` cursor moves included) and every `\r`, the 2.7 frame
dropped blank lines, and the BusyBox recording showed a tab expanded to spaces
(`"a       b"`) because the session left the tty's `tab3` on.

What these commands cannot show, so the model rests on the commands named:

1. No stock Zephyr command writes chosen bytes. That the shell turns a written
   `\n` into `\r\n` rests on the recorded commands; none in the recordings
   writes a `\r`. (A probe on 2026-11-13, not kept as a recording: `log
   status`, which writes `\r\n` itself through `shell_fprintf`, arrived as
   `\r\r\n`.) Whether a bare `\r` survives is **unmeasured**.
2. Every recorded non-empty Zephyr output ends with a newline, and empty output
   sends nothing (2.7's `devmem`). Whether the shell inserts `\r\n` before the
   prompt when output does not end in one is **unmeasured**.
3. The message-level colouring is removed as the three exact sequences `RED`,
   `YEL` and `RST`, wherever they fall. A command that itself writes one of
   them loses it: the bytes cannot tell it from the shell's.
4. No recorded Zephyr output ends in spaces; leading spaces (`help`) survive.

Releases: the model of each Zephyr frame is the same across every release that
shares it (3.7 and 4.4 for `zephyr` and `zephyr-serial`; 2.7 is alone on
`zephyr-inline`).
