import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { DigestFeed } from "./DigestFeed";
import type { DigestSummary } from "./api/digests";

const digest: DigestSummary = {
  id: "01a034ed-e100-7e73-ab06-1fecafdc495c",
  digest_date: "2026-08-24",
  status: "published",
  title: "AI Daily Digest — 24 August 2026",
};

const handlers = { onRetry: vi.fn(), onLoadMore: vi.fn() };

describe("DigestFeed states", () => {
  it("renders an accessible loading state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading loadingMore={false} error={null} nextCursor={null} {...handlers} />);
    expect(html).toContain("Loading published digests");
    expect(html).toContain('aria-busy="true"');
  });

  it("renders the unpublished empty state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error={null} nextCursor={null} {...handlers} />);
    expect(html).toContain("No published digests yet");
  });

  it("renders a retryable, user-safe error", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error="The digest service could not be reached." nextCursor={null} {...handlers} />);
    expect(html).toContain("Digests are temporarily unavailable");
    expect(html).toContain("Try again");
  });

  it("renders published API data and pagination", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[digest]} initialLoading={false} loadingMore={false} error={null} nextCursor="cursor" {...handlers} />);
    expect(html).toContain("AI Daily Digest — 24 August 2026");
    expect(html).toContain("24 August 2026");
    expect(html).toContain("Evidence-checked edition");
    expect(html).toContain("Load more digests");
  });
});
