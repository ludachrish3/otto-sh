// The Topology|List switcher (spec §View switcher): vendored button-border
// tabs. `active` comes from the HOSTING page, not internal state — the
// selectedWindowId lesson: a stored copy of a route-derived value drifts.
// Icons: Dataflow03 matches the palette's Topology command (commands.ts);
// List matches Untitled UI's informational-page Grid/List selector.
import { Dataflow03, List } from "@untitledui/icons";
import { useHashLocation } from "wouter/use-hash-location";

import { Tab, TabList, Tabs } from "@/components/application/tabs/tabs";

const ROUTES = { topology: "/", hosts: "/hosts" } as const;

export function ViewSwitcher({ active }: { active: keyof typeof ROUTES }) {
  const [, navigate] = useHashLocation();
  return (
    <Tabs
      data-testid="view-toggle"
      selectedKey={active}
      onSelectionChange={(key) => {
        if (key !== active) navigate(ROUTES[key as keyof typeof ROUTES]);
      }}
    >
      <TabList aria-label="View" type="button-border" size="sm">
        <Tab id="topology" icon={Dataflow03}>
          Topology View
        </Tab>
        <Tab id="hosts" icon={List}>
          List View
        </Tab>
      </TabList>
    </Tabs>
  );
}
