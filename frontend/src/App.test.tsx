// @vitest-environment jsdom

import { StrictMode, act } from "react";
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

function scopedButtonWithLabel(scope: Element, label: string): HTMLButtonElement {
  const button = scope.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (!button) throw new Error(`Button not found within scope: ${label}`);
  return button;
}

function updatesFeed(container: HTMLElement): HTMLElement {
  const section = container.querySelector<HTMLElement>("section.feed");
  if (!section) throw new Error("Updates feed section not found");
  return section;
}

function digestFeed(container: HTMLElement): HTMLElement {
  const section = container.querySelector<HTMLElement>("section.digestFeed");
  if (!section) throw new Error("Digest feed section not found");
  return section;
}

afterEach(() => {
  vi.useRealTimers();
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

  it("shows page 1 initially, fetches page 2 via next_cursor, and highlights the current page", async () => {
    const updateResponses = [
      jsonResponse([firstUpdate], "opaque-cursor"),
      jsonResponse([secondUpdate], "second-cursor"),
    ];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([], null);
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));

    await waitForText(container, "First source update");
    const feed = updatesFeed(container);
    expect(feed.querySelector('button[aria-label="Go to page 1"][aria-current="page"]')).toBeTruthy();
    expect(feed.querySelector('article.articleCard')).toBeTruthy();
    expect(feed.querySelectorAll('article.articleCard')).toHaveLength(1);

    await act(async () => scopedButtonWithLabel(feed, "Next page").click());
    await waitForText(container, "Second source update");

    expect(container.textContent).not.toContain("First source update");
    expect(feed.querySelector('button[aria-label="Go to page 2"][aria-current="page"]')).toBeTruthy();
    const updateCalls = vi.mocked(fetchMock).mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === "/v1/updates"
    ));
    expect(updateCalls).toHaveLength(2);
    const secondCallUrl = new URL(String(updateCalls[1]?.[0]));
    expect(secondCallUrl.searchParams.get("cursor")).toBe("opaque-cursor");

    await act(async () => root.unmount());
  });

  it("navigates to the correct page exactly once under StrictMode's double-invoked state updaters", async () => {
    // React (Strict Mode) intentionally double-invokes effects and state
    // updater functions to surface impurity, so responses are keyed by the
    // request's own cursor (deterministic given the input) rather than call
    // order -- a shift()-based queue would be consumed twice by the extra
    // mount/fetch StrictMode performs and would not exercise the real thing
    // under test. goToDigestsPage/goToUpdatesPage read the page cache from
    // the closure and call only the plain current-page setter -- no nested
    // setter-inside-setter -- so a double invocation must still land on
    // exactly page 2, not skip ahead or throw.
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([], null);
      const cursor = url.searchParams.get("cursor");
      if (cursor === "opaque-cursor") return jsonResponse([secondUpdate], null);
      return jsonResponse([firstUpdate], "opaque-cursor");
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<StrictMode><App /></StrictMode>));
    await waitForText(container, "First source update");

    const feed = updatesFeed(container);
    await act(async () => scopedButtonWithLabel(feed, "Next page").click());
    await waitForText(container, "Second source update");

    // Exactly page 2 is current -- no skip to a further page under the
    // double-invoked updater, and the stale first source update is gone.
    expect(feed.querySelector('button[aria-label="Go to page 2"][aria-current="page"]')).toBeTruthy();
    expect(feed.querySelector('button[aria-label="Go to page 3"]')).toBeNull();
    expect(container.textContent).not.toContain("First source update");

    // Clicking the cached page-1 number lands on exactly page 1 -- not page 0,
    // not left on page 2 -- proving goToUpdatesPage's own setter call is pure.
    await act(async () => scopedButtonWithLabel(feed, "Go to page 1").click());
    await waitForText(container, "First source update");
    expect(feed.querySelector('button[aria-label="Go to page 1"][aria-current="page"]')).toBeTruthy();
    expect(container.textContent).not.toContain("Second source update");

    await act(async () => root.unmount());
  });

  it("navigates back to a cached page with Previous without making another request", async () => {
    const updateResponses = [
      jsonResponse([firstUpdate], "opaque-cursor"),
      jsonResponse([secondUpdate], null),
    ];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([], null);
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, "First source update");

    const feed = updatesFeed(container);
    await act(async () => scopedButtonWithLabel(feed, "Next page").click());
    await waitForText(container, "Second source update");

    const callsAfterNext = vi.mocked(fetchMock).mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === "/v1/updates"
    )).length;

    await act(async () => scopedButtonWithLabel(feed, "Previous page").click());
    await waitForText(container, "First source update");
    expect(container.textContent).not.toContain("Second source update");
    expect(feed.querySelector('button[aria-label="Go to page 1"][aria-current="page"]')).toBeTruthy();

    const callsAfterPrevious = vi.mocked(fetchMock).mock.calls.filter(([input]) => (
      new URL(String(input)).pathname === "/v1/updates"
    )).length;
    expect(callsAfterPrevious).toBe(callsAfterNext);

    await act(async () => root.unmount());
  });

  it("disables Next once a fetched page returns next_cursor=null (terminal page)", async () => {
    const updateResponses = [jsonResponse([firstUpdate], null)];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([], null);
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, "First source update");

    // A single terminal page (next_cursor null, only one known page) renders no pager at all.
    expect(updatesFeed(container).querySelector("nav.pager")).toBeNull();

    await act(async () => root.unmount());
  });

  it("preserves the current page and cached items when a next-page request fails", async () => {
    const updateResponses = [
      jsonResponse([firstUpdate], "opaque-cursor"),
      new Response(null, { status: 503 }),
      jsonResponse([secondUpdate], null),
    ];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") return digestJsonResponse([], null);
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, "First source update");

    const feed = updatesFeed(container);
    await act(async () => scopedButtonWithLabel(feed, "Next page").click());
    await waitForText(container, "returned an error");

    // Current page indicator and cached page-1 content are unchanged after the failure.
    expect(feed.querySelector('button[aria-label="Go to page 1"][aria-current="page"]')).toBeTruthy();
    expect(container.textContent).toContain("First source update");
    expect(container.textContent).not.toContain("Second source update");

    // Retrying the same failed next-page request (via the inline error's retry button) succeeds.
    await act(async () => buttonWithText(feed, "Try again").click());
    await waitForText(container, "Second source update");

    await act(async () => root.unmount());
  });

  it("keeps digest and updates pagination independent of each other", async () => {
    const secondPageDigest: DigestSummary = {
      ...firstDigest,
      id: "01a034ed-e100-7e73-ab06-1fecafdc495f",
      title: "AI Daily Digest — page two edition",
    };
    const updateResponses = [
      jsonResponse([firstUpdate], "u-cursor-1"),
      jsonResponse([secondUpdate], null),
    ];
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") {
        const cursor = url.searchParams.get("cursor");
        if (cursor === "d-cursor-1") return digestJsonResponse([secondPageDigest], null);
        return digestJsonResponse([firstDigest], "d-cursor-1");
      }
      const response = updateResponses.shift();
      if (!response) throw new Error("Unexpected extra fetch");
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<App />));
    await waitForText(container, firstDigest.title);
    await waitForText(container, "First source update");

    // Advance only the digest feed to page 2.
    await act(async () => scopedButtonWithLabel(digestFeed(container), "Next page").click());
    await waitForText(container, secondPageDigest.title);

    // Updates feed must still be on page 1, unaffected.
    const updatesSection = updatesFeed(container);
    expect(updatesSection.textContent).toContain("First source update");
    expect(updatesSection.querySelector('button[aria-label="Go to page 1"][aria-current="page"]')).toBeTruthy();

    // Now advance only the updates feed to page 2.
    await act(async () => scopedButtonWithLabel(updatesSection, "Next page").click());
    await waitForText(container, "Second source update");

    // Digest feed must still show its own page 2, unaffected by the updates navigation.
    expect(container.textContent).toContain(secondPageDigest.title);
    expect(digestFeed(container).querySelector('button[aria-label="Go to page 2"][aria-current="page"]')).toBeTruthy();

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

  it("resets digest pagination to page 1, clears the cache, closes open detail, and never sends a stale cursor when period changes", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-12T12:00:00Z"));

    let resolveFilteredPage!: (value: Response) => void;
    const filteredPagePromise = new Promise<Response>((resolve) => {
      resolveFilteredPage = resolve;
    });

    const requestedDigestUrls: string[] = [];
    const secondDigest: DigestSummary = {
      id: "01a034ed-e100-7e73-ab06-1fecafdc495e",
      digest_date: "2026-09-08",
      status: "published",
      title: "Filtered Week Digest — 08 September 2026",
    };

    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") {
        requestedDigestUrls.push(url.href);
        const dateFrom = url.searchParams.get("date_from");
        if (dateFrom) {
          return filteredPagePromise;
        }
        return digestJsonResponse([firstDigest], "cursor.page2.all");
      }
      if (url.pathname === `/v1/digests/${firstDigest.id}`) {
        return new Response(JSON.stringify(digestDetail(firstDigest, "Initial claim detail")), {
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

    // Initial page has a cursor -> the pager's Next control is present and enabled.
    const feed = digestFeed(container);
    expect(scopedButtonWithLabel(feed, "Next page").disabled).toBe(false);

    // Open detail for firstDigest
    await act(async () => buttonWithLabel(
      container,
      `View details for ${firstDigest.title}`,
    ).click());
    await waitForText(container, "Initial claim detail");
    expect(container.textContent).toContain("Initial claim detail");

    // Change period dropdown to "week"
    const periodSelect = container.querySelector<HTMLSelectElement>("#digest-period");
    expect(periodSelect).toBeTruthy();

    await act(async () => {
      if (periodSelect) {
        periodSelect.value = "week";
        periodSelect.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    // IMMEDIATE assertions while filtered request is STILL PENDING:
    // 1. Proves open detail is closed and old items/detail are immediately removed
    expect(container.textContent).not.toContain("Initial claim detail");
    expect(container.textContent).not.toContain(firstDigest.title);
    expect(container.textContent).toContain("Loading published digests");

    // 2. Proves the old pager (with its cached cursor state) is immediately gone
    expect(digestFeed(container).querySelector("nav.pager")).toBeNull();

    // 3. Proves filtered query was dispatched without any stale cursor
    const filteredCall = requestedDigestUrls.find((call) => call.includes("date_from")) ?? "";
    const parsedFilteredUrl = new URL(filteredCall);
    expect(parsedFilteredUrl.searchParams.get("cursor")).toBeNull();
    expect(parsedFilteredUrl.searchParams.get("date_from")).toBe("2026-09-05");

    // Now resolve the filtered response
    await act(async () => {
      resolveFilteredPage(digestJsonResponse([secondDigest], null));
    });

    // Wait for filtered digest to appear
    await waitForText(container, secondDigest.title);
    expect(container.textContent).toContain(secondDigest.title);
    // Single terminal page after reset -> no pager rendered.
    expect(digestFeed(container).querySelector("nav.pager")).toBeNull();

    await act(async () => root.unmount());
  });

  it("closes an open digest detail when navigating to a different digest page", async () => {
    const secondPageDigest: DigestSummary = {
      ...firstDigest,
      id: "01a034ed-e100-7e73-ab06-1fecafdc495f",
      title: "AI Daily Digest — page two edition",
    };
    const fetchMock: FetchUpdates & FetchDigests = vi.fn(async (input) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/digests") {
        const cursor = url.searchParams.get("cursor");
        if (cursor === "d-cursor-1") return digestJsonResponse([secondPageDigest], null);
        return digestJsonResponse([firstDigest], "d-cursor-1");
      }
      if (url.pathname === `/v1/digests/${firstDigest.id}`) {
        return new Response(JSON.stringify(digestDetail(firstDigest, "Page one claim detail")), {
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

    await act(async () => buttonWithLabel(container, `View details for ${firstDigest.title}`).click());
    await waitForText(container, "Page one claim detail");

    await act(async () => scopedButtonWithLabel(digestFeed(container), "Next page").click());
    await waitForText(container, secondPageDigest.title);

    expect(container.textContent).not.toContain("Page one claim detail");
    expect(container.querySelector(".digestDetailPanel")).toBeNull();

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
