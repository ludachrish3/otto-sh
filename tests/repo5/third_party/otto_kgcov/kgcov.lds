/* kgcov.lds — applied at the consumer's intermediate link (ld -r), before
 * the kernel's own module linker script sees the module. Gathers every
 * constructor convention into one .init_array so the two sentinels bracket
 * the gcov constructors on every kernel and toolchain: the begin sentinel
 * (.init_array.0) sorts first, then gcc's or clang's .init_array.NNNNN,
 * then a .ctors toolchain's .ctors.NNNNN and .ctors, then the end sentinel
 * in plain .init_array. A no-op where the module linker script already
 * orders .init_array (kernel 4.0 on) and the toolchain emits it. */
SECTIONS {
    .init_array : { *(SORT(.init_array.*)) *(SORT(.ctors.*)) *(.ctors) *(.init_array) }
}
