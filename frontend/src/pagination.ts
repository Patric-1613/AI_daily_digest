// Client-side cursor page cache and numbered-window helpers.
//
// The API (ADR 0008) is keyset/cursor-only: it never exposes a total or a
// last-page number, and pagination Phase 1 explicitly excludes page numbers
// and backward cursors. A "page number" here is a purely client-side
// bookkeeping concept: pages are only ever appended sequentially (1, 2, 3, ...)
// as their cursor is followed forward, so the cache is always contiguous and
// `cache.size` is always the highest page actually fetched so far.

export interface PageEntry<T> {
  items: T[];
  nextCursor: string | null;
}

export type PageCache<T> = Map<number, PageEntry<T>>;

export const FIRST_PAGE = 1;

/** The highest page number actually fetched and cached. 0 if nothing is cached yet. */
export function maxKnownPage<T>(cache: PageCache<T>): number {
  return cache.size;
}

/** Whether Next can be followed from `currentPage`: that page is cached and its
 * next_cursor was non-null (never invented — only ever set from a real response). */
export function hasNextFrom<T>(cache: PageCache<T>, currentPage: number): boolean {
  return cache.get(currentPage)?.nextCursor != null;
}

/**
 * The last page is "known" only once the highest cached page's own response
 * carried `next_cursor: null` — never before. A highest cached page whose
 * cursor is still non-null is only the cache frontier (we simply haven't
 * followed it further yet), not a discovered final page, so this returns
 * `false` for it. The cache can never grow past a page once this is `true`
 * for it (Next is permanently disabled from that page), so the known
 * terminal page is always exactly `maxKnownPage(cache)` when this holds.
 */
export function isTerminalPageKnown<T>(cache: PageCache<T>): boolean {
  const highest = maxKnownPage(cache);
  return highest > 0 && cache.get(highest)?.nextCursor === null;
}

/** The confirmed final page number, or `null` if the cache has not yet
 * reached a page whose own `next_cursor` was `null`. Never guessed from the
 * current cache frontier alone -- see `isTerminalPageKnown`'s docstring. */
export function getKnownTerminalPage<T>(cache: PageCache<T>): number | null {
  return isTerminalPageKnown(cache) ? maxKnownPage(cache) : null;
}

export interface PageWindow {
  pages: number[];
  showLeadingEllipsis: boolean;
  showTrailingEllipsis: boolean;
}

/**
 * At most `maxVisible` numeric page buttons, centred on `current`, drawn only
 * from `[1, highestCachedPage]` — every number in the window is therefore
 * always a page that has actually been fetched. An ellipsis is shown only
 * when it hides *known, cached* pages on that side (never to suggest an
 * unknown last page beyond `highestCachedPage`).
 *
 * This function only ever knows about the cache frontier, not whether it is
 * the true final page -- see `isTerminalPageKnown`/`getKnownTerminalPage`.
 * Callers that also know a confirmed terminal page decide separately whether
 * to surface it after a trailing ellipsis (see `Pager.tsx`).
 */
export function getPageWindow(
  current: number,
  highestCachedPage: number,
  maxVisible = 5,
): PageWindow {
  if (highestCachedPage <= 0) {
    return { pages: [], showLeadingEllipsis: false, showTrailingEllipsis: false };
  }
  if (highestCachedPage <= maxVisible) {
    return {
      pages: Array.from({ length: highestCachedPage }, (_, index) => index + 1),
      showLeadingEllipsis: false,
      showTrailingEllipsis: false,
    };
  }

  const half = Math.floor(maxVisible / 2);
  let start = Math.min(Math.max(current - half, 1), highestCachedPage - maxVisible + 1);
  start = Math.max(start, 1);
  const end = start + maxVisible - 1;

  return {
    pages: Array.from({ length: maxVisible }, (_, index) => start + index),
    showLeadingEllipsis: start > 1,
    showTrailingEllipsis: end < highestCachedPage,
  };
}
