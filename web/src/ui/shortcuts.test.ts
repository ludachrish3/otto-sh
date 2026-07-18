import { describe, expect, it } from "vitest";

import {
  type Binding,
  detectMac,
  formatBindingFor,
  matchesBindingFor,
  shouldSuppressSlash,
} from "./shortcuts";

const MOD_I: Binding = { key: "i", mod: true };
const SLASH: Binding = { key: "/" };

function keyEvent(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

describe("detectMac", () => {
  it("recognizes mac-family platforms", () => {
    expect(detectMac("MacIntel")).toBe(true);
    expect(detectMac("iPhone")).toBe(true);
    expect(detectMac("Win32")).toBe(false);
    expect(detectMac("Linux x86_64")).toBe(false);
    expect(detectMac("")).toBe(false);
  });
});

describe("formatBindingFor", () => {
  it("formats chords per platform", () => {
    expect(formatBindingFor(MOD_I, true)).toBe("⌘I");
    expect(formatBindingFor(MOD_I, false)).toBe("Ctrl I");
    expect(formatBindingFor({ key: ".", mod: true }, true)).toBe("⌘.");
    expect(formatBindingFor({ key: ".", mod: true }, false)).toBe("Ctrl .");
  });
  it("formats the bare slash identically everywhere", () => {
    expect(formatBindingFor(SLASH, true)).toBe("/");
    expect(formatBindingFor(SLASH, false)).toBe("/");
  });
});

describe("matchesBindingFor", () => {
  it("matches ctrl chords on non-mac and rejects meta", () => {
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true }), MOD_I, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "I", ctrlKey: true }), MOD_I, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "i", metaKey: true }), MOD_I, false)).toBe(false);
  });
  it("matches meta chords on mac and rejects ctrl", () => {
    expect(matchesBindingFor(keyEvent({ key: "i", metaKey: true }), MOD_I, true)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true }), MOD_I, true)).toBe(false);
  });
  it("rejects bare letters, extra modifiers, and wrong keys", () => {
    expect(matchesBindingFor(keyEvent({ key: "i" }), MOD_I, false)).toBe(false);
    expect(
      matchesBindingFor(keyEvent({ key: "i", ctrlKey: true, shiftKey: true }), MOD_I, false),
    ).toBe(false);
    expect(
      matchesBindingFor(keyEvent({ key: "i", ctrlKey: true, altKey: true }), MOD_I, false),
    ).toBe(false);
    expect(matchesBindingFor(keyEvent({ key: "s", ctrlKey: true }), MOD_I, false)).toBe(false);
  });
  it("matches the bare slash only without modifiers", () => {
    expect(matchesBindingFor(keyEvent({ key: "/" }), SLASH, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "/", ctrlKey: true }), SLASH, false)).toBe(false);
  });
  it("matches the bare slash with shiftKey set (intl layouts type / as Shift+7)", () => {
    expect(matchesBindingFor(keyEvent({ key: "/", shiftKey: true }), SLASH, false)).toBe(true);
  });
  it("still rejects shiftKey on a mod chord", () => {
    expect(
      matchesBindingFor(keyEvent({ key: "i", ctrlKey: true, shiftKey: true }), MOD_I, false),
    ).toBe(false);
  });
});

describe("shouldSuppressSlash", () => {
  it("suppresses inside editable targets", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    expect(shouldSuppressSlash(input, false)).toBe(true);
    expect(shouldSuppressSlash(textarea, false)).toBe(true);
    expect(shouldSuppressSlash(editable, false)).toBe(true);
  });
  it("suppresses inside an open dialog or menu subtree", () => {
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const child = document.createElement("span");
    dialog.appendChild(child);
    document.body.appendChild(dialog);
    expect(shouldSuppressSlash(child, false)).toBe(true);
    dialog.remove();
  });
  it("suppresses while the palette overlay is open regardless of target", () => {
    expect(shouldSuppressSlash(document.body, true)).toBe(true);
  });
  it("suppresses for an SVG target inside a dialog subtree (gate is Element, not HTMLElement)", () => {
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    dialog.appendChild(svg);
    document.body.appendChild(dialog);
    expect(shouldSuppressSlash(svg, false)).toBe(true);
    dialog.remove();
  });
  it("fires on a plain body target", () => {
    expect(shouldSuppressSlash(document.body, false)).toBe(false);
    expect(shouldSuppressSlash(null, false)).toBe(false);
  });
});
