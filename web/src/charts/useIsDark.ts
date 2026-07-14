// Charts render to canvas and cannot follow CSS `dark:` variants — this
// hook observes the <html> class that theme.ts toggles (`.dark-mode` —
// Untitled UI's class; see theme.ts) so chart options rebuild on theme
// changes.
import { useEffect, useState } from "react";

export function useIsDark(): boolean {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark-mode"));
  useEffect(() => {
    const observer = new MutationObserver(() => {
      setDark(document.documentElement.classList.contains("dark-mode"));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return dark;
}
