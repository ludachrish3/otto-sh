// web/src/ui/Kbd.tsx
// The keycap chip. Class list mirrors the vendored InputBase shortcut
// keycap (components/base/input/input.tsx — rounded / px-1 py-px / text-xs
// font-medium text-quaternary / inset ring-secondary) so authored hint
// sites are pixel-identical to the vendored one; duplicated here because
// the vendored file cannot be edited to export it.
export function Kbd({ children }: { children: string }) {
  return (
    <kbd
      aria-hidden
      className="pointer-events-none rounded px-1 py-px font-sans text-xs font-medium text-quaternary ring-1
        ring-secondary select-none ring-inset"
    >
      {children}
    </kbd>
  );
}
