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
    change: {
      id: "01a034ed-e100-74d1-8508-247704ced118",
      company: "Anthropic",
      product: "Claude 3.5 Sonnet",
      field: "context_window_tokens",
      change_type: "increased",
      previous_value: "100000",
      current_value: "200000",
    },
    citations: [{
      snapshot_id: "01a032cd-23e0-76d3-a27c-f608ccc02226",
      canonical_url: "https://www.anthropic.com/news/claude-2-1",
      source_title: "Introducing Claude 2.1",
    }],
  }],
};

const handlers = {
  onRetry: vi.fn(),
  onGoToPage: vi.fn(),
  onPrevious: vi.fn(),
  onNext: vi.fn(),
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

const pagerState = { currentPage: 1, highestCachedPage: 1, terminalPage: null, hasNext: false };

describe("DigestFeed states", () => {
  it("renders an accessible loading state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading loadingMore={false} error={null} {...pagerState} {...detailState} {...handlers} />);
    expect(html).toContain("Loading published digests");
    expect(html).toContain('aria-busy="true"');
  });

  it("renders the unpublished empty state", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error={null} {...pagerState} {...detailState} {...handlers} />);
    expect(html).toContain("No published digests yet");
  });

  it("renders a retryable, user-safe error", () => {
    const html = renderToStaticMarkup(<DigestFeed digests={[]} initialLoading={false} loadingMore={false} error="The digest service could not be reached." {...pagerState} {...detailState} {...handlers} />);
    expect(html).toContain("Digests are temporarily unavailable");
    expect(html).toContain("Try again");
  });

  it("renders published API data and a numbered pager with the current page highlighted", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      currentPage={1}
      highestCachedPage={2}
      terminalPage={null}
      hasNext
      {...detailState}
      {...handlers}
    />);
    expect(html).toContain("AI Daily Digest — 24 August 2026");
    expect(html).toContain("24 August 2026");
    expect(html).toContain("Evidence-checked edition");
    expect(html).toContain('<nav class="pager" aria-label="Digest pagination">');
    expect(html).toContain('aria-label="Go to page 1"');
    expect(html).toContain('aria-current="page"');
    expect(html).toContain('aria-label="Go to page 2"');
    expect(html).toContain(`aria-label="View details for ${digest.title}"`);
    expect(html).toContain('aria-expanded="false"');
  });

  it("omits the pager entirely when there is only one known, terminal page", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      currentPage={1}
      highestCachedPage={1}
      terminalPage={null}
      hasNext={false}
      {...detailState}
      {...handlers}
    />);
    expect(html).not.toContain('class="pager"');
  });

  it("shows the inline retry banner and keeps the pager's current page during a load-more error", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error="The digest service could not be reached."
      currentPage={1}
      highestCachedPage={1}
      terminalPage={null}
      hasNext
      {...detailState}
      {...handlers}
    />);
    expect(html).toContain('class="inlineError"');
    expect(html).toContain('class="pager"');
    expect(html).toContain('aria-label="Go to page 1"');
    expect(html).toContain('aria-current="page"');
  });

  it("renders the selected detail loading state", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      {...pagerState}
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
      {...pagerState}
      selectedDigestId={digest.id}
      detail={null}
      detailLoading={false}
      detailError="The digest details could not be reached."
      {...handlers}
    />);

    expect(html).toContain("Digest details are unavailable");
    expect(html).toContain("Try again");
    expect(html).not.toContain("No public source link is available");
  });

  it("renders an explicit empty-claims state", () => {
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      {...pagerState}
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
      {...pagerState}
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
    expect(html).toContain("context window tokens:");
    expect(html).toContain("100000");
    expect(html).toContain("200000");
    expect(html).toContain("changed to");
  });

  it("omits the before-and-after line when a claim has no linked change", () => {
    const unlinkedDetail = {
      ...detail,
      claims: [{ ...detail.claims[0]!, change: null }],
    };
    const html = renderToStaticMarkup(<DigestFeed
      digests={[digest]}
      initialLoading={false}
      loadingMore={false}
      error={null}
      {...pagerState}
      selectedDigestId={digest.id}
      detail={unlinkedDetail}
      detailLoading={false}
      detailError={null}
      {...handlers}
    />);

    expect(html).not.toContain("claimChange");
    expect(html).not.toContain("context window tokens:");
  });

});
