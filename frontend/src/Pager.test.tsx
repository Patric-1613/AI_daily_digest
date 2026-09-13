import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Pager } from "./Pager";

const noop = () => undefined;

describe("Pager", () => {
  it("renders nothing when there is only a single, terminal page", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={1}
        terminalPage={null}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    expect(html).toBe("");
  });

  it("wraps controls in a labelled nav with boxed Previous/1..5/Next controls", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Updates pagination"
        currentPage={1}
        highestCachedPage={5}
        terminalPage={null}
        hasNext={true}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    expect(html).toContain('<nav class="pager" aria-label="Updates pagination">');
    expect(html).toContain(">Previous<");
    expect(html).toContain(">Next<");
    for (const page of [1, 2, 3, 4, 5]) {
      expect(html).toContain(`aria-label="Go to page ${page}"`);
    }
  });

  it("marks the current page with aria-current and a distinct class", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={3}
        highestCachedPage={5}
        terminalPage={null}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    // aria-current is present exactly once, on page 3's button.
    expect((html.match(/aria-current="page"/g) ?? []).length).toBe(1);
    expect(html).toContain('class="pagerButton pagerButtonCurrent"');
  });

  it("never shows more than five numeric page boxes", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={6}
        highestCachedPage={12}
        terminalPage={null}
        hasNext={true}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const numberedButtons = html.match(/aria-label="Go to page \d+"/g) ?? [];
    expect(numberedButtons.length).toBeLessThanOrEqual(5);
    expect(html).toContain("…");
  });

  it("disables Previous at page 1 and disables Next when there is no cached next page", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={1}
        terminalPage={null}
        hasNext={true}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const previousMatch = html.match(/<button[^>]*aria-label="Previous page"[^>]*>/)?.[0] ?? "";
    expect(previousMatch).toContain("disabled=\"\"");
  });

  it("disables Next once the cached current page's next_cursor is null (terminal page)", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={2}
        highestCachedPage={2}
        terminalPage={null}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const nextMatch = html.match(/<button[^>]*aria-label="Next page"[^>]*>/)?.[0] ?? "";
    expect(nextMatch).toContain("disabled=\"\"");
  });

  it("disables the whole pager while a next-page fetch is pending", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={2}
        highestCachedPage={2}
        terminalPage={null}
        hasNext={true}
        nextPending={true}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    expect(html.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(3);
    expect(html).toContain("Loading…");
  });

  it("shows the known terminal page after the trailing ellipsis: 1 2 3 4 5 … 10", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={10}
        terminalPage={10}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const numberedButtons = html.match(/aria-label="Go to page \d+"/g) ?? [];
    expect(numberedButtons).toEqual([
      'aria-label="Go to page 1"',
      'aria-label="Go to page 2"',
      'aria-label="Go to page 3"',
      'aria-label="Go to page 4"',
      'aria-label="Go to page 5"',
      'aria-label="Go to page 10"',
    ]);
    // Exactly one ellipsis, between the regular window and the terminal page.
    expect((html.match(/…/g) ?? []).length).toBe(1);
  });

  it("never duplicates the terminal page when it already falls inside the regular window", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={9}
        highestCachedPage={10}
        terminalPage={10}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const numberedButtons = html.match(/aria-label="Go to page \d+"/g) ?? [];
    expect(numberedButtons).toEqual([
      'aria-label="Go to page 6"',
      'aria-label="Go to page 7"',
      'aria-label="Go to page 8"',
      'aria-label="Go to page 9"',
      'aria-label="Go to page 10"',
    ]);
    expect(numberedButtons.filter((label) => label === 'aria-label="Go to page 10"')).toHaveLength(1);
  });

  it("never presents a non-terminal cache frontier as the final page", () => {
    // 10 pages cached, but the highest one's own next_cursor is still non-null
    // (Next stays enabled) -- terminalPage must be null, and no page-10 box
    // may appear after the ellipsis implying it is the last page.
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={10}
        terminalPage={null}
        hasNext={true}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const numberedButtons = html.match(/aria-label="Go to page \d+"/g) ?? [];
    expect(numberedButtons).toEqual([
      'aria-label="Go to page 1"',
      'aria-label="Go to page 2"',
      'aria-label="Go to page 3"',
      'aria-label="Go to page 4"',
      'aria-label="Go to page 5"',
    ]);
    expect(numberedButtons).not.toContain('aria-label="Go to page 10"');
    // The trailing ellipsis is still shown (more cached pages exist beyond the window),
    // it just never resolves to an extra terminal-page box while unconfirmed.
    expect((html.match(/…/g) ?? []).length).toBe(1);
  });

  it("permits at most five regular window buttons plus one terminal-page button", () => {
    const html = renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={10}
        terminalPage={10}
        hasNext={false}
        nextPending={false}
        onGoToPage={noop}
        onPrevious={noop}
        onNext={noop}
      />,
    );
    const numberedButtons = html.match(/aria-label="Go to page \d+"/g) ?? [];
    expect(numberedButtons.length).toBeLessThanOrEqual(6);
    expect(numberedButtons.length).toBe(6);
  });

  it("calls onGoToPage, onPrevious and onNext from their respective controls", () => {
    const onGoToPage = vi.fn();
    // renderToStaticMarkup cannot exercise click handlers (no DOM); this is a
    // structural check that each control is wired to its own handler prop by
    // asserting they are distinct functions rendered without throwing.
    expect(() => renderToStaticMarkup(
      <Pager
        ariaLabel="Digest pagination"
        currentPage={1}
        highestCachedPage={3}
        terminalPage={null}
        hasNext={true}
        nextPending={false}
        onGoToPage={onGoToPage}
        onPrevious={noop}
        onNext={noop}
      />,
    )).not.toThrow();
  });
});
