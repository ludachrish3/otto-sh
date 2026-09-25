# Check verdicts and reports

otto's setup checks survey your lab and tell you which of otto's features
work there. Today there is one, {doc}`otto link check <link/check>`. They
all answer in the same words, which this page defines: the verdict on each
row, the reason an `unmeasured` row gives, the exit code, the `--report`
file, and the labels that say how your hosts compare with the versions otto
has been proven on.

## Verdicts

Every row of a check's table carries exactly one verdict:

| Verdict | What happened | What it means for you | Fails the run |
| --- | --- | --- | --- |
| `pass` | otto applied the feature and measured it working as configured. | You can rely on this feature on this host. | no |
| `fail` | The host accepted the command, but the measurement came out wrong. | Either otto or the host misbehaves. This is the row otto's developers most want a report on. | yes |
| `unsupported` | The host's tool or kernel rejected the command. | The host lacks a capability, such as a kernel module or a tc feature. The rejected command and the tool's complaint are printed beneath the row. | yes |
| `unmeasured` | otto could not get evidence either way. | Nothing is known about this feature on this host yet. The row always gives a [reason](#why-a-row-is-unmeasured). | no |
| `skipped` | Something this row depends on didn't happen, so the row never ran. | The row's detail, or the hint beneath it, says what was missing. | no |

The split between `fail` and `unsupported` matters. `unsupported` means "your
host cannot do this". `fail` means "your host said yes, and the result was
wrong", which usually means otto has a bug on your kind of host.

## Why a row is unmeasured

An `unmeasured` row always carries one of these reason codes. The row's
detail column starts with the code, followed by whatever otto measured or
knows, for example `noisy-baseline: measured loss 20%, σ 9.8 ms` or
`missing-tool: needs socat or python3 on test1`. A code with nothing to add
is printed alone (`no-clock`).

| Code | Why | What to do |
| --- | --- | --- |
| `missing-tool` | A tool the measurement needs isn't installed. The detail names it, and the host. | Install it and run again. |
| `noisy-baseline` | Before measuring, otto takes a baseline with nothing impaired. That baseline lost packets or varied too much to compare against. The detail shows the loss and spread otto saw. | Run again when the host or path is quieter. If it keeps happening, look for what is loading the host or dropping packets on the path. |
| `no-reply-oracle` | The target runs nothing of otto's that could answer, so a UDP payload cannot be confirmed. | Nothing on your side. This is a known gap in what otto can prove. `otto link check` never reports it. |
| `no-clock` | A timed probe printed no elapsed time. For the socat probe that means the host's bash has no `$EPOCHREALTIME` (bash older than 5.0). | Install python3 on the host (otto prefers it), or a newer bash. |

`unmeasured` and `skipped` never fail a run. They mean "no evidence", not
"broken".

## Exit codes

| Code | When |
| --- | --- |
| `0` | Nothing failed. `pass`, `unmeasured` and `skipped` rows only. |
| `1` | Any row is `fail` or `unsupported`. Also when the check could not produce a table: the target was refused (for example, a link otto cannot impair), a host did not answer at all, or the link or host you named does not exist. A host that answers but whose probe stalls is not this case: that probe's row is `fail`, and the rest of the table is still produced. |
| `2` | A usage error, such as an unknown `--feature` name. Nothing was contacted. |

A dry run (`otto -n …`) exits `0` after printing its plan. It still exits
`1` for a target that is refused or doesn't exist, since it can tell that
without contacting anything.

## The report file

Nothing is written unless you ask. `--report PATH` writes everything the
check found as JSON, and the command prints `report: PATH` when it's done. A
dry run writes no report and says so.

Attach this file when you [open an issue](https://github.com/ludachrish3/otto-sh/issues).
It carries the evidence otto's developers need, so they don't have to ask you
for it. It is also how otto learns about environments it hasn't been proven
on (see {doc}`link/known-good`).

The file is one JSON object:

- `schema` is always `"otto-check/1"`. It changes only if the layout does.
- `kind` names the check (`"link"`).
- `otto_version` is the otto that ran the check.
- `proven_revision` is the revision of the proven-range list your hosts were
  labeled against.
- `result` is the check's whole result. It holds each host's fingerprint
  (kernel, architecture, userland, privilege, which tools were found and
  their versions, and the raw probe output) and, for each row, the verdict
  and reason code, the measured and wanted values, the exact commands otto
  ran, and what they printed. A refused target has an empty host list and
  the refusal and its hint instead.

The report contains addresses: each host's management address, the
addresses on the interfaces checked, and target addresses inside the
commands. It also contains host ids and the login user. Nothing in it is a
secret, and otto has no option to leave any of it out. If you'd rather not
post them, edit them out before you attach the file. Keep the structure
intact, and replace each address consistently so the report still makes
sense. Addresses sit inside the recorded commands as well as in the
fingerprint fields. A timed probe's command, for example, which the
terminal shows as a `ran:` line, names the far endpoint:

```text
live ran: bash -c 's=$EPOCHREALTIME; echo x | socat -T 15 -t 15 - TCP:10.10.202.12:5205,connect-timeout=5 | grep -qx x; …'
```

Here `10.10.202.12` is the address to replace, in this command and in every
other place it appears.

## Proven-range labels

Every host a check fingerprints gets a `proven range:` line under its
heading, with one label per component (for example, its iproute2 version or
its kernel). The label compares your version with the versions otto has been
proven on, listed on {doc}`link/known-good`.

| Label | Meaning |
| --- | --- |
| `within` | otto has been proven on this version, or, for a component with a version order such as iproute2, on versions both older and newer than it. |
| `older` | Older than every proven version. |
| `newer` | Newer than every proven version. |
| `outside` | For a component without a version order (the architecture, the userland), a value otto hasn't been proven on. |
| `unknown` | otto couldn't read or order your version, or nothing has been proven for this component yet. |

A label never changes a verdict. It tells you how much otto's own testing
covers your host. Any report is welcome, and one from a host that reads
`older`, `newer` or `outside` is especially useful, even when every row
passed, because that is how the proven range grows. `unknown` alone is
common: it often means otto has nothing recorded for that component yet.
