import { describe, expect, it } from "vitest";
import {
  getPreviewKind,
  listPreviewKinds,
  registerPreviewKind,
  type PreviewRenderer,
} from "../../src/templates/registry.js";

/**
 * Registry contract tests. We import via the public ``registry`` module
 * directly so the SDK's auto-registration of ``react`` / ``html`` (which
 * happens on ``templates/index.ts`` import) doesn't pollute the table.
 */

describe("preview kind registry", () => {
  const dummy: PreviewRenderer = () => null;

  it("register + lookup round-trip", () => {
    registerPreviewKind("test:latex", dummy);
    expect(getPreviewKind("test:latex")).toBe(dummy);
  });

  it("returns undefined for unknown kinds", () => {
    expect(getPreviewKind("does-not-exist")).toBeUndefined();
  });

  it("last call wins (apps can override built-in kinds)", () => {
    const a: PreviewRenderer = () => null;
    const b: PreviewRenderer = () => null;
    registerPreviewKind("test:override", a);
    registerPreviewKind("test:override", b);
    expect(getPreviewKind("test:override")).toBe(b);
  });

  it("listPreviewKinds includes every registered kind", () => {
    registerPreviewKind("test:list-a", dummy);
    registerPreviewKind("test:list-b", dummy);
    const kinds = listPreviewKinds();
    expect(kinds).toContain("test:list-a");
    expect(kinds).toContain("test:list-b");
  });
});
