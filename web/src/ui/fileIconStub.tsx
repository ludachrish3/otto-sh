// The vendored Untitled UI EmptyState wires `@untitledui/file-icons` into an
// `EmptyState.FileTypeIcon` slot that nothing in otto renders, and the icon
// set is ~220 kB of every shipped bundle. Vendored source must stay
// byte-identical (web/README.md, "vendored source"; enforced by
// scripts/check_untitledui_hash.sh), so the dependency is cut on OUR side:
// both Vite configs alias the package to this file at bundle time. tsc and
// knip keep seeing the real package — only the bundles see the stub.
//
// The props mirror the two the vendored slot forwards so a future caller
// of EmptyState.FileTypeIcon type-checks the same either way.
interface FileIconProps {
  type?: string;
  variant?: string;
  className?: string;
}

export function FileIcon(_props: FileIconProps): null {
  return null;
}
