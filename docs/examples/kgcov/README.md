# otto_kgcov

A GPL companion kernel module that gives out-of-tree modules gcov coverage
on a stock kernel built without `CONFIG_GCOV_KERNEL`. It provides the
`__gcov_*` symbols an instrumented object references, an accumulator per
instrumented object (a dump never double counts, and two dumps of the same
run add correctly), and a debugfs control file per registered module.

Build against the running kernel's headers:

```sh
make -C docs/examples/kgcov
```

Set `KDIR` to point at a different kernel's build tree.

A consumer becomes coverage-instrumented in three steps:

1. **Sentinels.** Link two tiny objects — one built from a file containing
   only `KGCOV_SENTINEL_BEGIN;` and one from a file containing only
   `KGCOV_SENTINEL_END;` — first and last in the consumer's object list, so
   they bound the `.gcov_info` section gcc emits for every other object.
2. **Flags.** Compile every instrumented object with `$(KGCOV_CFLAGS)` from
   `consumer.mk`, which also adds `-I$(KGCOV)` via `ccflags-y` so `kgcov.h`
   is on the include path — nothing else to pass.
3. **Macros.** Call `KGCOV_DECLARE()` at file scope, `KGCOV_INIT()` as the
   first statement of the module's init routine, and `KGCOV_EXIT()` as the
   last statement of its exit routine.

Once registered, a write to
`/sys/kernel/debug/otto_kgcov/<module>/dump` writes each instrumented
object's `.gcda` file under the module's `gcov_dir` parameter; a write to
`.../reset` zeroes the accumulated counters. The exit routine's own
coverage is captured by the dump `KGCOV_EXIT()` performs on unregister.

A worked consumer following these three steps lives at
[`tests/repo5/kmod/demo/`](../../../tests/repo5/kmod/demo/) — a bounded
queue driven from debugfs, built in place by `tests/repo5/build.sh` against
the library this directory's own `build.sh` builds out of tree. It lives in
the SUT repo rather than here because otto's coverage capture anchors every
measured file to a committed git blob under the repo that declares the
`[[products]]` entry.
