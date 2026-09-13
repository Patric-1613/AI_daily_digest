import { describe, expect, it } from "vitest";
import {
  getKnownTerminalPage,
  getPageWindow,
  hasNextFrom,
  isTerminalPageKnown,
  maxKnownPage,
} from "./pagination";
import type { PageCache } from "./pagination";

function cacheOfSize(size: number, terminal: boolean): PageCache<string> {
  const cache: PageCache<string> = new Map();
  for (let page = 1; page <= size; page += 1) {
    const isHighest = page === size;
    cache.set(page, { items: [`item-${page}`], nextCursor: isHighest && terminal ? null : `cursor-${page}` });
  }
  return cache;
}

describe("getPageWindow", () => {
  it("returns nothing when no page has been fetched yet", () => {
    expect(getPageWindow(1, 0)).toEqual({
      pages: [],
      showLeadingEllipsis: false,
      showTrailingEllipsis: false,
    });
  });

  it("shows every known page with no ellipsis when five or fewer are known", () => {
    expect(getPageWindow(2, 3)).toEqual({
      pages: [1, 2, 3],
      showLeadingEllipsis: false,
      showTrailingEllipsis: false,
    });
    expect(getPageWindow(1, 5)).toEqual({
      pages: [1, 2, 3, 4, 5],
      showLeadingEllipsis: false,
      showTrailingEllipsis: false,
    });
  });

  it("shows at most five numeric pages, centred on the current page", () => {
    const window = getPageWindow(5, 9);
    expect(window.pages).toHaveLength(5);
    expect(window.pages).toEqual([3, 4, 5, 6, 7]);
    expect(window.showLeadingEllipsis).toBe(true);
    expect(window.showTrailingEllipsis).toBe(true);
  });

  it("never invents an ellipsis beyond the known last page", () => {
    // Current page IS the last known page (e.g. next_cursor was null there) --
    // the trailing side must never suggest more, unknown pages exist.
    const window = getPageWindow(9, 9);
    expect(window.pages).toEqual([5, 6, 7, 8, 9]);
    expect(window.showLeadingEllipsis).toBe(true);
    expect(window.showTrailingEllipsis).toBe(false);
  });

  it("clamps the window at the start without a leading ellipsis", () => {
    const window = getPageWindow(1, 9);
    expect(window.pages).toEqual([1, 2, 3, 4, 5]);
    expect(window.showLeadingEllipsis).toBe(false);
    expect(window.showTrailingEllipsis).toBe(true);
  });
});

describe("maxKnownPage / hasNextFrom", () => {
  it("reports zero known pages for an empty cache", () => {
    const cache: PageCache<string> = new Map();
    expect(maxKnownPage(cache)).toBe(0);
    expect(hasNextFrom(cache, 1)).toBe(false);
  });

  it("reports the highest cached page and whether it can advance", () => {
    const cache: PageCache<string> = new Map([
      [1, { items: ["a"], nextCursor: "c1" }],
      [2, { items: ["b"], nextCursor: null }],
    ]);
    expect(maxKnownPage(cache)).toBe(2);
    expect(hasNextFrom(cache, 1)).toBe(true);
    expect(hasNextFrom(cache, 2)).toBe(false);
  });
});

describe("isTerminalPageKnown / getKnownTerminalPage", () => {
  it("is unknown for an empty cache", () => {
    const cache: PageCache<string> = new Map();
    expect(isTerminalPageKnown(cache)).toBe(false);
    expect(getKnownTerminalPage(cache)).toBeNull();
  });

  it("is unknown while the highest cached page's own next_cursor is non-null -- a cache", () => {
    // frontier is NOT the same thing as a discovered final page.
    const cache = cacheOfSize(10, false);
    expect(isTerminalPageKnown(cache)).toBe(false);
    expect(getKnownTerminalPage(cache)).toBeNull();
    expect(maxKnownPage(cache)).toBe(10); // frontier reached, but not confirmed final
  });

  it("is known, and equals the highest cached page, once that page's next_cursor is null", () => {
    const cache = cacheOfSize(10, true);
    expect(isTerminalPageKnown(cache)).toBe(true);
    expect(getKnownTerminalPage(cache)).toBe(10);
  });
});
