// web/src/shell/exportdialog.test.tsx
// The export name dialog against the real stores: the name it offers, the
// download it makes under the name typed, and the acknowledgement it posts
// for a server source. fetch, URL.createObjectURL and the anchor's click are
// mocked; the store and the anchor's `download` are the assertion surface.
// What an acknowledgement may claim across a resync is pinned in
// bootstrap.resync.test.ts.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { synthSession } from "../__tests__/_synth";
import type { MonitorSourceState } from "../api/export.gen";
import { useReviewStore } from "../data/reviewStore";
import { useUiStore } from "../ui/uiStore";
import { ExportDialog } from "./ExportDialog";
import { exportFileName, fileStem, LIVE_EXPORT_NAME, openExportDialog } from "./ImportExport";

describe("exportFileName", () => {
  it.each([
    ["run.json", 0, "run.json"],
    ["run.json", 2, "run-edited.json"],
    ["run-edited.json", 1, "run-edited.json"],
    ["cap.db", 0, "cap.json"],
    ["messung-öl.json", 1, "messung-öl-edited.json"],
  ])("%s with %i unsaved gives %s", (sourceName, unsaved, expected) => {
    expect(exportFileName({ mode: "review", sourceName, unsaved })).toBe(expected);
  });

  it.each([
    ["run.json", "run"],
    ["cap.tar.db", "cap.tar"],
    ["README", "README"],
    [".json", ".json"],
  ])("fileStem(%s) is %s", (name, stem) => {
    expect(fileStem(name)).toBe(stem);
  });

  it("a live source, or no name, gives the live default", () => {
    expect(exportFileName({ mode: "live", sourceName: "run.json", unsaved: 3 })).toBe(
      LIVE_EXPORT_NAME,
    );
    expect(exportFileName({ mode: null, sourceName: null, unsaved: 0 })).toBe(LIVE_EXPORT_NAME);
    expect(LIVE_EXPORT_NAME).toBe("otto-monitor-export.json");
  });
});

const JSON_SOURCE: MonitorSourceState = {
  mode: "review",
  kind: "json",
  source: "run.json",
  token: "tok-1",
  editable: true,
  unsaved: 2,
  revision: 7,
  observed_at: "2026-11-12T10:00:00.000000+00:00",
};

let downloads: string[];
let fetchMock: ReturnType<typeof vi.fn>;

function loadJsonSource(): void {
  const actions = useReviewStore.getState().actions;
  useReviewStore.setState({
    sessions: [
      { ...synthSession({ hosts: 1, seriesPerHost: 1, ticks: 1, intervalS: 5 }), id: "s" },
    ],
    sourceName: "run.json",
    // The sessions hold revision 7, as an installed snapshot's would.
    covered: { token: JSON_SOURCE.token, revision: 7 },
  });
  actions.setSourceState(JSON_SOURCE);
}

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Waits for the acknowledgement the export posted to settle, answered or
 * refused (acknowledgeExport swallows both). */
const ackSettled = () =>
  Promise.allSettled(fetchMock.mock.results.map((r) => r.value as Promise<unknown>));

beforeEach(() => {
  downloads = [];
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
  fetchMock = vi.fn().mockResolvedValue(okJson({ ...JSON_SOURCE, unsaved: 0 }));
  vi.stubGlobal("fetch", fetchMock);
  loadJsonSource();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  const actions = useReviewStore.getState().actions;
  actions.setPendingSource(null);
  actions.setSourceState(null);
  useReviewStore.setState({
    sessions: [],
    sourceName: null,
    warnings: [],
    lastSourceStamp: null,
    covered: null,
  });
  useUiStore.getState().actions.closeExportDialog();
});

function openDialog(): void {
  openExportDialog();
  render(<ExportDialog />);
}

describe("ExportDialog", () => {
  it("opens pre-filled with the export file name", () => {
    openDialog();
    expect((screen.getByTestId("export-name") as HTMLInputElement).value).toBe("run-edited.json");
  });

  it("confirm downloads under the typed name and acknowledges the revision", async () => {
    const user = userEvent.setup();
    openDialog();
    const input = screen.getByTestId("export-name");
    await user.clear(input);
    await user.type(input, "mine.json");
    await user.click(screen.getByTestId("export-confirm"));
    expect(downloads).toEqual(["mine.json"]);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/source/saved");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ token: "tok-1", revision: 7 });
    expect(useUiStore.getState().exportDialogOpen).toBe(false);
  });

  it("opening focuses the field with the stem selected", () => {
    openDialog();
    const input = screen.getByTestId("export-name") as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd]).toEqual([0, "run-edited".length]);
  });

  it("Enter confirms the dialog, with no click first", async () => {
    const user = userEvent.setup();
    openDialog();
    expect(document.activeElement).toBe(screen.getByTestId("export-name"));
    await user.keyboard("{Enter}");
    expect(downloads).toEqual(["run-edited.json"]);
    expect(useUiStore.getState().exportDialogOpen).toBe(false);
  });

  it("Enter while an IME is composing does not confirm", () => {
    openDialog();
    const input = screen.getByTestId("export-name");
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(downloads).toEqual([]);
    expect(useUiStore.getState().exportDialogOpen).toBe(true);
  });

  it("blank or whitespace name disables Export", async () => {
    const user = userEvent.setup();
    openDialog();
    const input = screen.getByTestId("export-name");
    const confirm = screen.getByTestId("export-confirm");
    await user.clear(input);
    expect(confirm.hasAttribute("disabled") || confirm.getAttribute("data-disabled") !== null).toBe(
      true,
    );
    await user.type(input, "   ");
    await user.click(confirm);
    await user.keyboard("{Enter}");
    expect(downloads).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useUiStore.getState().exportDialogOpen).toBe(true);
  });

  it("cancel downloads nothing and acknowledges nothing", async () => {
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(downloads).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useUiStore.getState().exportDialogOpen).toBe(false);
  });

  it("a failed acknowledgement still downloads and keeps the marker", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByTestId("export-confirm"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    await ackSettled();
    expect(downloads).toEqual(["run-edited.json"]);
    expect(useReviewStore.getState().unsaved).toBe(2);
    expect(useReviewStore.getState().warnings).toEqual([]);
  });

  it("a stale-token acknowledgement is refused by the server and swallowed by the tab", async () => {
    const newer: MonitorSourceState = {
      ...JSON_SOURCE,
      token: "tok-2",
      source: "other.json",
      observed_at: "2026-11-12T10:00:05.000000+00:00",
    };
    fetchMock.mockResolvedValue(
      okJson({ error: "the source changed", code: "source_changed", source_state: newer }, 409),
    );
    const before = useReviewStore.getState();
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByTestId("export-confirm"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    await ackSettled();
    expect(downloads).toEqual(["run-edited.json"]);
    const after = useReviewStore.getState();
    expect(after.warnings).toEqual([]);
    expect(after.source).toBe(before.source);
    expect(after.pendingSource).toBe(before.pendingSource);
    expect(after.lastSourceStamp).toBe(before.lastSourceStamp);
    expect(after.sessions).toBe(before.sessions);
  });

  it("no acknowledgement while a source is pending", async () => {
    useReviewStore.getState().actions.setPendingSource({
      ...JSON_SOURCE,
      token: "tok-2",
      observed_at: "2026-11-12T10:00:05.000000+00:00",
    });
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByTestId("export-confirm"));
    // The acknowledgement is posted in the click's own turn, or never.
    expect(downloads).toEqual(["run-edited.json"]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("any installed server source is acknowledged: the server decides what it saves", async () => {
    useReviewStore.getState().actions.setSourceState({
      ...JSON_SOURCE,
      kind: "db",
      source: "cap.db",
      observed_at: "2026-11-12T10:00:06.000000+00:00",
    });
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByTestId("export-confirm"));
    expect(downloads).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ token: "tok-1", revision: 7 });
  });

  it("no acknowledgement with no server source", async () => {
    useReviewStore.getState().actions.setSourceState(null);
    const user = userEvent.setup();
    openDialog();
    await user.click(screen.getByTestId("export-confirm"));
    expect(downloads).toEqual(["run.json"]);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
