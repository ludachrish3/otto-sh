// parseExportDocument judges `format` by the declared read list
// (web/src/data/exportFormat.ts), never by a literal of its own. With the
// list swapped for [2], a version-2 document is read and a version-1
// document is refused; a reader still comparing with `1` fails both. The
// writer, documentFromSessions, stamps the mocked EXPORT_FORMAT, not a `1`.
import { describe, expect, it, vi } from "vitest";

vi.mock("../data/exportFormat", () => ({
  MONITOR_EXPORT_READ_VERSIONS: [2],
  MONITOR_EXPORT_WRITE_VERSIONS: [2],
  EXPORT_FORMAT: 2,
}));

// Imported AFTER the mock so exportDoc.ts picks up the swapped list.
const { documentFromSessions, ExportParseError, parseExportDocument } = await import(
  "../data/exportDoc"
);

describe("parseExportDocument follows the declared read list", () => {
  it("reads a version the list allows", () => {
    const { sessions, warnings } = parseExportDocument(JSON.stringify({ format: 2, sessions: [] }));
    expect(sessions).toEqual([]);
    expect(warnings).toEqual([]);
  });

  it("refuses a version the list no longer holds", () => {
    expect(() => parseExportDocument(JSON.stringify({ format: 1, sessions: [] }))).toThrow(
      ExportParseError,
    );
  });
});

describe("documentFromSessions follows the declared write version", () => {
  it("stamps the declared write version", () => {
    expect(documentFromSessions([]).format).toBe(2);
  });
});
