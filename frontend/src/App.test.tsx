// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { getPeriodDateRange } from "./App";
import type { DigestDetail, DigestSummary, FetchDigests } from "./api/digests";
import type { FetchUpdates, UpdateSummary } from "./api/updates";
import { publicConfig } from "./config";

const firstUpdate: UpdateSummary = {
  id: "01a032cd-23e0-7cf2-83e9-6d6f9f9326f2",
  source_id: "openai_news",
  publisher: "OpenAI",
  title: "First source update",
  canonical_url: "https://openai.com/news/first",
  published_at: "2026-09-07T09:00:00Z",
  first_fetched_at: "2026-09-07T09:01:00Z",
  event_id: null,
  tags: ["model_release"],
  language: "en",
  latest_snapshot_id: null,
};

const secondUpdate: UpdateSummary = {
  ...firstUpdate,
  id: "01a032cd-23e0-7cf2-83e9-6d6f9f9326f3",
  title: "Second source update",
  canonical_url: "https://openai.com/news/second",
};

const firstDigest: DigestSummary = {
  id: "01a034ed-e100-7e73-ab06-1fecafdc495c",
  digest_date: "2026-09-07",
  status: "published",
  title: "AI Daily Digest — 7 September 2026",
};

const secondDigest: DigestSummary = {
  ...firstDigest,
  id: "01a034ed-e100-7e73-ab06-1fecafdc495d",
  title: "AI Daily Digest — second edition",
};

function digestDetail(digest: DigestSummary, claimText: string): DigestDetail {
  return {
    ...digest,
    claims: [{
      id: `${digest.id.slice(0, -1)}1`,
      text: claimText,
      validation_status: "supported",
      citations: [{
        snapshot_id: "01a032cd-23e0-76d3-a27c-f608ccc02226",
        canonical_url: "https://www.anthropic.com/news/claude-2-1",
        source_title: "Introducing Claude 2.1",
      }],
    }],
  };
}

function jsonResponse(items: UpdateSummary[], nextCursor: string | null): Response {
  return new Response(JSON.stringify({ items, next_cursor: nextCursor }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function digestJsonResponse(items: DigestSummary[], nextCursor: string | null): Response {
  return new Response(JSON.stringify({ items, next_cursor: nextCursor }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function waitForText(container: HTMLElement, text: string): Promise<void> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (container.textContent?.includes(text)) return;
    await act(async () => Promise.resolve());
  }
  throw new Error(`Expected mounted App to render: ${text}`);
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")]
    .find((candidate) => candidate.textContent?.includes(text));
  if (!button) throw new Error(`Button not found: ${text}`);
  return button;
}

function buttonWithLabel(container: HTMLElement, label: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (!button) throw new Error(`Button not found: ${label}`);
  return button;
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("AI Daily Digest shell", () => {
  it("uses the documented local API origin by default", () => {
    expect(publicConfig.apiBaseUrl).toBe("http://localhost:8000");
    expect(publicConfig.subscriptionsEnabled).toBe(false);
  });

  it("hides subscription controls by default and renders them only when enabled", () => {
    const disabledHtml = renderToStaticMarkup(<App />);
    const enabledHtml = renderToStaticMarkup(<App subscriptionsEnabled />);

    expect(disabledHtml).not.toContain("you@example.com");
    expect(disabledHtml).not.toContain(">Subscribe<");
    expect(enabledHtml).toContain("you@example.com");
    expect(enabledHtml).toContain(">Subscribe<");
  });

  it("renders the core editorial sections", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).toContain("The signal in AI");
    expect(html).toContain("Source-backed AI industry monitoring");
    expect(html).toContain("0 digests loaded");
    expect(html).toContain("0 updates loaded");
    expect(html).toContain("Loading published digests");
    expect(html).toContain("Loading source updates");
    expect(html).not.toContain("Cursor-paginated");
  });

  it("does not present non-functional controls or fabricated trust figures", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).not.toContain("Illustrative model explorer");
    expect(html).not.toContain("Ask about today");
    expect(html).not.toContain("40 claims checked");
    expect(html).not.toContain("38 sourced");
    expect(html).not.toContain("How this works");
  });

  it("runs initial failure, abort-aware retry, and cursor load more through the mounted App", async () => {
    const updateResponses = [
      new Response(null, { status: 503 }),
      jsonResponse([firstUpdate], "opaque-cursor"),
      jsonResponse([secondUpdate], null),
    ];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([firstDigest], null);
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));

    await waitForText(container, "Updates are temporarily unavailable");
    const initialUpdateCall = vi.mocked(fetchMock).mock.calls.find(([input]) => (
      new URL(String(input)).pathname === "/v1/updates"
    ));
    const initialSignal = initialUpdateCall?.[1]?.signal;

    await act(async () => buttonWithText(container, "Try again").click());
    await waitForText(container, "First source update");
    expect(initialSignal?.aborted).toBe(true);

    await act(async () => buttonWithText(container, "Load more updates").click());
    await waitForText(container, "Second source update");

    expect(container.textContent).toContain("AI Daily Digest — 7 September 2026");
    expect(container.querySelectorAll("article.articleCard")).toHaveLength(2);
    const updateCalls = vi.mocked(fetchMock).mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === "/v1/updates"
    ));
    expect(updateCalls).toHaveLength(3);
    const loadMoreUrl = new URL(String(updateCalls[2]?.[0]));
    expect(loadMoreUrl.searchParams.get("cursor")).toBe("opaque-cursor");

    await act(async () => root.unmount());
  });

  it("aborts an outstanding retry when the App unmounts", async () => {
    let updateCallCount = 0;
    const observedSignals: AbortSignal[] = [];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input, init) => {
      if (new URL(String(input)).pathname === "/v1/digests") {
        return digestJsonResponse([], null);
      }
      updateCallCount += 1;
      if (updateCallCount === 1) {
        return new Response(null, { status: 503 });
      }
      if (init?.signal) observedSignals.push(init.signal);
      return new Promise<Response>(() => undefined);
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, "Updates are temporarily unavailable");

    await act(async () => buttonWithText(container, "Try again").click());
    expect(observedSignals).toHaveLength(1);
    await act(async () => root.unmount());

    expect(observedSignals[0]?.aborted).toBe(true);
  });

  it("aborts stale detail requests and ignores a late response", async () => {
    type PendingDetail = {
      signal: AbortSignal | null;
      resolve: (response: Response) => void;
    };
    const pending = new Map<string, PendingDetail>();
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") {
        return digestJsonResponse([firstDigest, secondDigest], null);
      }
      if (url.pathname.startsWith("/v1/digests/")) {
        const digestId = url.pathname.split("/").at(-1) ?? "";
        return new Promise<Response>((resolve) => {
          pending.set(digestId, { signal: init?.signal ?? null, resolve });
        });
      }
      return jsonResponse([], null);
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, secondDigest.title);

    await act(async () => buttonWithLabel(
      container,
      `View details for ${firstDigest.title}`,
    ).click());
    expect(pending.has(firstDigest.id)).toBe(true);

    await act(async () => buttonWithLabel(
      container,
      `View details for ${secondDigest.title}`,
    ).click());
    expect(pending.get(firstDigest.id)?.signal?.aborted).toBe(true);
    expect(pending.has(secondDigest.id)).toBe(true);

    await act(async () => pending.get(secondDigest.id)?.resolve(new Response(JSON.stringify(
      digestDetail(secondDigest, "Second digest claim"),
    ), { status: 200 })));
    await waitForText(container, "Second digest claim");

    await act(async () => pending.get(firstDigest.id)?.resolve(new Response(JSON.stringify(
      digestDetail(firstDigest, "Stale first digest claim"),
    ), { status: 200 })));
    await act(async () => Promise.resolve());

    expect(container.textContent).toContain("Second digest claim");
    expect(container.textContent).not.toContain("Stale first digest claim");
    await act(async () => root.unmount());
  });

  it("surfaces a citation-integrity failure through the retryable detail error", async () => {
    let detailAttempts = 0;
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([firstDigest], null);
      if (url.pathname.startsWith("/v1/digests/")) {
        detailAttempts += 1;
        if (detailAttempts === 1) {
          const invalidDetail = digestDetail(firstDigest, "Must not render as a successful claim");
          invalidDetail.claims[0]!.citations = [{
            snapshot_id: "unsafe",
            canonical_url: "javascript:alert(1)",
            source_title: "Unsafe source",
          }];
          return new Response(JSON.stringify(invalidDetail), { status: 200 });
        }
        return new Response(JSON.stringify(digestDetail(firstDigest, "Recovered claim")), {
          status: 200,
        });
      }
      return jsonResponse([], null);
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, firstDigest.title);

    await act(async () => buttonWithLabel(
      container,
      `View details for ${firstDigest.title}`,
    ).click());
    await waitForText(container, "Digest details are unavailable");
    expect(container.textContent).not.toContain("No public source link is available");
    expect(container.textContent).not.toContain("Must not render as a successful claim");

    await act(async () => buttonWithText(container, "Try again").click());
    await waitForText(container, "Recovered claim");
    expect(detailAttempts).toBe(2);

    await act(async () => buttonWithLabel(
      container,
      `Close details for ${firstDigest.title}`,
    ).click());
    expect(container.textContent).not.toContain("Recovered claim");
    expect(buttonWithLabel(container, `View details for ${firstDigest.title}`)).toBeTruthy();
    await act(async () => root.unmount());
  });

  it("filters digests when period select changes", async () => {
    const requestedDigestUrls: string[] = [];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") {
        requestedDigestUrls.push(url.href);
        return digestJsonResponse([firstDigest], null);
      }
      return jsonResponse([], null);
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, firstDigest.title);

    const periodSelect = container.querySelector<HTMLSelectElement>("#digest-period");
    expect(periodSelect).toBeTruthy();

    await act(async () => {
      if (periodSelect) {
        periodSelect.value = "week";
        periodSelect.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    const lastDigestCall = requestedDigestUrls.at(-1) ?? "";
    const parsedUrl = new URL(lastDigestCall);
    expect(parsedUrl.searchParams.has("date_from")).toBe(true);

    await act(async () => root.unmount());
  });
});

describe("getPeriodDateRange helper", () => {
  const fixedNow = new Date("2026-09-12T12:00:00Z");

  it("returns null bounds for 'all'", () => {
    expect(getPeriodDateRange("all", fixedNow)).toEqual({ date_from: null, date_to: null });
  });

  it("computes bounds for 'today'", () => {
    expect(getPeriodDateRange("today", fixedNow)).toEqual({
      date_from: "2026-09-12",
      date_to: "2026-09-13",
    });
  });

  it("computes bounds for 'yesterday'", () => {
    expect(getPeriodDateRange("yesterday", fixedNow)).toEqual({
      date_from: "2026-09-11",
      date_to: "2026-09-12",
    });
  });

  it("computes bounds for 'week'", () => {
    expect(getPeriodDateRange("week", fixedNow)).toEqual({
      date_from: "2026-09-05",
      date_to: null,
    });
  });

  it("computes bounds for 'month'", () => {
    expect(getPeriodDateRange("month", fixedNow)).toEqual({
      date_from: "2026-08-13",
      date_to: null,
    });
  });

  it("computes bounds for 'year'", () => {
    expect(getPeriodDateRange("year", fixedNow)).toEqual({
      date_from: "2025-09-12",
      date_to: null,
    });
  });
});
