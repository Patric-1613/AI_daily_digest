import { Fragment } from "react";
import { getPageWindow } from "./pagination";

interface PagerProps {
  ariaLabel: string;
  currentPage: number;
  /** The highest page actually fetched and cached so far -- the cache
   * frontier. This is NOT necessarily the final page; see `terminalPage`. */
  highestCachedPage: number;
  /** The confirmed final page (that page's own response had
   * `next_cursor: null`), or `null` if that has not been discovered yet.
   * Passed separately from `highestCachedPage` so the cache frontier is
   * never presented as the last page before it is actually known to be
   * one -- see `pagination.ts`'s `getKnownTerminalPage`. */
  terminalPage: number | null;
  hasNext: boolean;
  nextPending: boolean;
  onGoToPage: (page: number) => void;
  onPrevious: () => void;
  onNext: () => void;
}

function pageButton(
  page: number,
  currentPage: number,
  nextPending: boolean,
  onGoToPage: (page: number) => void,
) {
  const isCurrent = page === currentPage;
  return (
    <li key={page}>
      <button
        type="button"
        className={`pagerButton${isCurrent ? " pagerButtonCurrent" : ""}`}
        onClick={() => onGoToPage(page)}
        disabled={nextPending}
        aria-disabled={nextPending}
        aria-current={isCurrent ? "page" : undefined}
        aria-label={`Go to page ${page}`}
      >
        {page}
      </button>
    </li>
  );
}

export function Pager({
  ariaLabel,
  currentPage,
  highestCachedPage,
  terminalPage,
  hasNext,
  nextPending,
  onGoToPage,
  onPrevious,
  onNext,
}: PagerProps) {
  if (highestCachedPage <= 1 && !hasNext) return null;

  const { pages, showLeadingEllipsis, showTrailingEllipsis } = getPageWindow(
    currentPage,
    highestCachedPage,
  );
  // The known terminal page is always <= highestCachedPage (the cache can
  // never grow past it once discovered) -- only surface it after the
  // trailing ellipsis when it is genuinely outside the regular window, so it
  // is never duplicated.
  const terminalPageAfterEllipsis = terminalPage !== null && !pages.includes(terminalPage)
    ? terminalPage
    : null;
  // While a Next fetch is in flight, the whole pager (not just Next) is
  // disabled -- otherwise a click on Previous or a numbered button could
  // race the in-flight fetch's eventual setCurrentPage() and be silently
  // overridden once that response lands.
  const previousDisabled = currentPage <= 1 || nextPending;
  const nextDisabled = !hasNext || nextPending;

  return (
    <nav className="pager" aria-label={ariaLabel}>
      <ul className="pagerList">
        <li>
          <button
            type="button"
            className="pagerButton pagerEdge"
            onClick={onPrevious}
            disabled={previousDisabled}
            aria-disabled={previousDisabled}
            aria-label="Previous page"
          >
            Previous
          </button>
        </li>

        {showLeadingEllipsis ? (
          <li className="pagerEllipsis" aria-hidden="true">&hellip;</li>
        ) : null}

        {pages.map((page) => (
          <Fragment key={page}>{pageButton(page, currentPage, nextPending, onGoToPage)}</Fragment>
        ))}

        {showTrailingEllipsis ? (
          <li className="pagerEllipsis" aria-hidden="true">&hellip;</li>
        ) : null}

        {terminalPageAfterEllipsis !== null
          ? pageButton(terminalPageAfterEllipsis, currentPage, nextPending, onGoToPage)
          : null}

        <li>
          <button
            type="button"
            className="pagerButton pagerEdge"
            onClick={onNext}
            disabled={nextDisabled}
            aria-disabled={nextDisabled}
            aria-label="Next page"
          >
            {nextPending ? "Loading…" : "Next"}
          </button>
        </li>
      </ul>
    </nav>
  );
}
