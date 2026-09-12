import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { DigestFeed } from "./DigestFeed";
import type { DigestDetail, DigestSummary } from "./api/digests";

const digest: DigestSummary = {
  id: "01a034ed-e100-7e73-ab06-1fecafdc495c",
  digest_date: "2026-08-24",
  status: "published",
  title: "AI Daily Digest — 24 August 2026",
};

const detail: DigestDetail = {
  ...digest,
  claims: [{
    id: "01a034ed-e100-74d1-8508-247704ced117",
    text: "Claude increased its context window from 100,000 to 200,000 tokens.",
    validation_status: "supported",
    citation_snapshot_ids: [],
    citations: [{
      snapshot_id: "01a032cd-23e0-76d3-a27c-f608ccc02226",
      canonical_url: "https://www.anthropic.com/news/claude-2-1",
      source_title: "Introducing Claude 2.1",
    }],
  }],
};

const handlers = {
  onRetry: vi.fn(),
  onLoadMore: vi.fn(),
  onSelectDigest: vi.fn(),
  onCloseDetail: vi.fn(),
  onRetryDetail: vi.fn(),
};

const detailState = {
  selectedDigestId: null,
  detail: null,
  detailLoading: false,
  detailError: null,
};

describe("DigestFeed states", () => {
  it("renders an accessible loading state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading loadingMore={false} error={null} nextCursor={null} {...detailState} {...handlers} />);
    expect(html).toContain("Loading published digests");
    expect(html).toContain('aria-busy="true"');
  });

  it("renders the unpublished empty state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error={null} nextCursor={null} {...detailState} {...handlers} />);
    expect(html).toContain("No published digests yet");
  });

  it("renders a retryable, user-safe error", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error="The digest service could not be reached." nextCursor={null} {...detailState} {...handlers} />);
    expect(html).toContain("Digests are temporarily unavailable");
    expect(html).toContain("Try again");
  });

  it("renders published API data and pagination", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[digest]} initialLoading={false} loadingMore={false} error={null} nextCursor="cursor" {...detailState} {...handlers} />);
    expect(html).toContain("AI Daily Digest — 24 August 2026");
    expect(html).toContain("24 August 2026");
    expect(html).toContain("Evidence-checked edition");
    expect(html).toContain("Load more digests");
    expect(html).toContain(`aria-label="View details for ${digest.title}"`);
    expect(html).toContain('aria-expanded="false"');
  });

  it("renders the selected detail loading state", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      nextCursor={null}
      selectedDigestId={digest.id}
      detail={null}
      detailLoading
      detailError={null}
      {...handlers}
    />);

    expect(html).toContain("Loading claims and citations");
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain("Close details");
  });

  it("renders a retryable detail error", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      nextCursor={null}
      selectedDigestId={digest.id}
      detail={null}
      detailLoading={false}
      detailError="The digest details could not be reached."
      {...handlers}
    />);

    expect(html).toContain("Digest details are unavailable");
    expect(html).toContain("Try again");
  });

  it("renders an explicit empty-claims state", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      nextCursor={null}
      selectedDigestId={digest.id}
      detail={{ ...detail, claims: [] }}
      detailLoading={false}
      detailError={null}
      {...handlers}
    />);

    expect(html).toContain("No claims are available for this published digest");
  });

  it("renders response claims, statuses, and accessible official links", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      nextCursor={null}
      selectedDigestId={digest.id}
      detail={detail}
      detailLoading={false}
      detailError={null}
      {...handlers}
    />);

    expect(html).toContain("100,000 to 200,000 tokens");
    expect(html).toContain("supported");
    expect(html).toContain("Introducing Claude 2.1");
    expect(html).toContain('href="https://www.anthropic.com/news/claude-2-1"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer noopener"');
    expect(html).toContain('aria-label="Read official source: Introducing Claude 2.1"');
  });

  it("does not invent a link for snapshot-id-only claims", () => {
    const noLinks = {
      ...detail,
      claims: [{ ...detail.claims[0]!, citations: [] }],
    };
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      nextCursor={null}
      selectedDigestId={digest.id}
      detail={noLinks}
      detailLoading={false}
      detailError={null}
      {...handlers}
    />);

    expect(html).toContain("No public source link is available for this claim");
    expect(html).not.toContain("<a ");
  });
});
