// web/src/ui/CommandLayer.tsx
// Mounted INSIDE App's <Router> (spec §Wiring): the registry needs
// navigation, so shortcuts + palette exist only once data is loaded —
// the EmptyState import screen keeps its own explicit buttons.
import { CommandMenu } from "./CommandMenu";
import { useCommands } from "./commands";
import { useGlobalShortcuts } from "./useGlobalShortcuts";

export function CommandLayer() {
  const commands = useCommands();
  useGlobalShortcuts(commands);
  return <CommandMenu commands={commands} />;
}
