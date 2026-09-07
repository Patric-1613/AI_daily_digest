import { describe, expect, it, vi } from "vitest";
import {
  fetchUpdatesPage,
  mergeUpdates,
  safeSourceUrl,
  UpdatesApiError,
} from "./updates";
import type { FetchUpdates, UpdateSummary } from "./updates";

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
  latest_snapshot_id: "01a032cd-23e0-76d3-a27c-f608ccc02226",
};

describe("updates API client", () => {
  it("requests the typed first page from the configured API", async () => {
    const requestedUrls: string[] = [];
    const fetchMock: FetchUpdates = vi.fn(async (input) => {
      requestedUrls.push(String(input));
      return new Response(JSON.stringify({
        items: [update],
        next_cursor: "signed-cursor",
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });

    const page = await fetchUpdatesPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock,
    });

    expect(page).toEqual({ items: [update], next_cursor: "signed-cursor" });
    const requestedUrl = new URL(requestedUrls[0] ?? "");
    expect(requestedUrl.href).toBe("https://api.example.com/v1/updates?limit=12");
  });

  it("passes an opaque cursor without interpreting it", async () => {
    const requestedUrls: string[] = [];
    const fetchMock: FetchUpdates = vi.fn(async (input) => {
      requestedUrls.push(String(input));
      return new Response(JSON.stringify({
        items: [],
        next_cursor: null,
      }), { status: 200 });
    });

    await fetchUpdatesPage({
      apiBaseUrl: "https://api.example.com/",
      cursor: "payload.signature",
      fetchImpl: fetchMock,
    });

    const requestedUrl = new URL(requestedUrls[0] ?? "");
    expect(requestedUrl.searchParams.get("cursor")).toBe("payload.signature");
  });

  it("rejects malformed success payloads at the network boundary", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      items: [{ title: "Missing required fields" }],
      next_cursor: null,
    }), { status: 200 }));

    await expect(fetchUpdatesPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock as FetchUpdates,
    })).rejects.toThrow(UpdatesApiError);
  });

  it("returns a user-safe error for unsuccessful responses", async () => {
    const fetchMock = vi.fn(async () => new Response("secret internal detail", { status: 503 }));

    await expect(fetchUpdatesPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock as FetchUpdates,
    })).rejects.toMatchObject({ status: 503, message: "The updates service returned an error. Please try again." });
  });
});

describe("updates feed helpers", () => {
  it("merges cursor pages without rendering duplicate IDs", () => {
    const second = { ...update, id: "01a032cd-23e0-7cf2-83e9-6d6f9f9326f3" };
    expect(mergeUpdates([update], [update, second, second])).toEqual([update, second]);
  });

  it("allows HTTP source links and refuses executable or malformed URLs", () => {
    expect(safeSourceUrl("https://openai.com/news/example")).toBe("https://openai.com/news/example");
    expect(safeSourceUrl("javascript:alert(1)")).toBeNull();
    expect(safeSourceUrl("not a URL")).toBeNull();
  });
});
