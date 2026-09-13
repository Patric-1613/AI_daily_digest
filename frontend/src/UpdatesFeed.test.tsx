import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { UpdateSummary } from "./api/updates";
import { UpdatesFeed } from "./UpdatesFeed";

const update: UpdateSummary = {
  id: "01a032cd-23e0-7cf2-83e9-6d6f9f9326f2",
  source_id: "openai_news",
  publisher: "OpenAI",
  title: "Example announcement",
  canonical_url: "https://openai.com/news/example",
  published_at: "2026-08-24T08:00:00Z",
  first_fetched_at: "2026-08-24T08:05:00Z",
  event_id: null,
  tags: ["model_release"],
  language: "en",
  latest_snapshot_id: null,
};

const handlers = { onRetry: vi.fn(), onGoToPage: vi.fn(), onPrevious: vi.fn(), onNext: vi.fn() };
const pagerState = { currentPage: 1, highestCachedPage: 1, terminalPage: null, hasNext: false };

describe("UpdatesFeed states", () => {
  it("renders an accessible loading state", () => {
    const html = renderToStaticMarkup(<UpdatesFeed updates={[]} initialLoading loadingMore={false} error={null} {...pagerState} {...handlers} />);
    expect(html).toContain("Loading source updates");
    expect(html).toContain('aria-busy="true"');
  });

  it("renders an empty state", () => {
    const html = renderToStaticMarkup(<UpdatesFeed updates={[]} initialLoading={false} loadingMore={false} error={null} {...pagerState} {...handlers} />);
    expect(html).toContain("No updates yet");
  });

  it("renders a retryable error without exposing response content", () => {
    const html = renderToStaticMarkup(<UpdatesFeed updates={[]} initialLoading={false} loadingMore={false} error="The updates service could not be reached." {...pagerState} {...handlers} />);
    expect(html).toContain("Updates are temporarily unavailable");
    expect(html).toContain("Try again");
  });

  it("renders API data, descriptive source attribution and a numbered pager", () => {
    const html = renderToStaticMarkup(<UpdatesFeed
      updates={[update]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      currentPage={2}
      highestCachedPage={3}
      terminalPage={null}
      hasNext
      {...handlers}
    />);
    expect(html).toContain("Example announcement");
    expect(html).toContain("Read at OpenAI");
    expect(html).toContain("model release");
    expect(html).toContain('<nav class="pager" aria-label="Updates pagination">');
    expect(html).toContain('aria-label="Go to page 2"');
    expect(html).toContain('aria-current="page"');
  });

  it("omits the pager entirely when there is only one known, terminal page", () => {
    const html = renderToStaticMarkup(<UpdatesFeed
      updates={[update]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      {...pagerState}
      {...handlers}
    />);
    expect(html).not.toContain('class="pager"');
  });

  it("does not render an unsafe source URL as a link", () => {
    const unsafeUpdate = { ...update, canonical_url: "javascript:alert(1)" };
    const html = renderToStaticMarkup(<UpdatesFeed updates={[unsafeUpdate]} initialLoading={false} loadingMore={false} error={null} {...pagerState} {...handlers} />);
    expect(html).toContain("Source link unavailable");
    expect(html).not.toContain("javascript:alert");
  });
});
