// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { DigestSummary, FetchDigests } from "./api/digests";
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

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("AI Daily Digest shell", () => {
  it("uses the documented local API origin by default", () => {
    expect(publicConfig.apiBaseUrl).toBe("http://localhost:8000");
  });

  it("renders the core editorial sections", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).toContain("The signal in AI");
    expect(html).toContain("Source-backed AI industry monitoring");
    expect(html).toContain("0 digests loaded");
    expect(html).toContain("0 updates loaded");
    expect(html).toContain("Illustrative model explorer");
    expect(html).toContain("Ask about today");
    expect(html).toContain("Illustrative trust metrics");
    expect(html).toContain("Loading published digests");
    expect(html).toContain("Loading source updates");
  });

  it.each(["Claude", "GPT-4", "Gemini", "DeepSeek", "Llama", "Grok"])(
    "includes the %s model family",
    (model) => {
      expect(renderToStaticMarkup(<App />)).toContain(model);
    },
  );

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
});
