import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TabSpec } from "../api/types.gen";
import EventTable from "../components/EventTable";
import { useMonitorStore } from "../store";

const TAB: TabSpec = {
  id: "syslog",
  label: "Syslog",
  metrics: [],
  kind: "table",
  columns: ["proc", "message"],
};

function seed(): void {
  useMonitorStore.setState({
    selectedHost: "host1",
    logEvents: {
      "host1/syslog": [
        {
          timestamp: "2026-07-04T12:00:00+00:00",
          host: "host1",
          tab: "syslog",
          fields: { proc: "sshd", message: "older" },
        },
        {
          timestamp: "2026-07-04T12:00:05+00:00",
          host: "host1",
          tab: "syslog",
          fields: { proc: "cron", message: "newer" },
        },
      ],
    },
  });
}

afterEach(cleanup);

describe("EventTable", () => {
  it("renders declared columns and newest-first rows for the selected host", () => {
    seed();
    render(<EventTable tab={TAB} />);
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["Time", "proc", "message"]);
    const cells = screen
      .getAllByRole("row")
      .slice(1)
      .map((tr) => tr.textContent);
    expect(cells[0]).toContain("newer");
    expect(cells[1]).toContain("older");
    expect(cells[0]).toContain("12:00:05"); // UTC time cell
  });

  it("substring filter narrows rows", () => {
    seed();
    render(<EventTable tab={TAB} />);
    fireEvent.change(screen.getByPlaceholderText("Filter rows…"), { target: { value: "sshd" } });
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + 1 match
  });

  it("shows nothing for a host without rows", () => {
    seed();
    useMonitorStore.setState({ selectedHost: "host2" });
    render(<EventTable tab={TAB} />);
    expect(screen.getAllByRole("row")).toHaveLength(1); // header only
  });
});
