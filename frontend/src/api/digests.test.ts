import { describe, expect, it, vi } from "vitest";
import {
  DigestsApiError,
  fetchDigestsPage,
  mergeDigests,
} from "./digests";
import type { DigestSummary, FetchDigests } from "./digests";

const digest: DigestSummary = {
  id: "01a034ed-e100-7e73-ab06-1fecafdc495c",
  digest_date: "2026-08-24",
  status: "published",
  title: "AI Daily Digest — 24 August 2026",
};

describe("digests API client", () => {
  it("requests the published digest feed from the configured API", async () => {
    const requestedUrls: string[] = [];
    const fetchMock: FetchDigests = vi.fn(async (input) => {
      requestedUrls.push(String(input));
      return new Response(JSON.stringify({
        items: [digest],
        next_cursor: "signed-cursor",
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });

    const page = await fetchDigestsPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock,
    });

    expect(page).toEqual({ items: [digest], next_cursor: "signed-cursor" });
    expect(requestedUrls).toHaveLength(1);
    expect(new URL(requestedUrls[0] ?? "").href).toBe(
      "https://api.example.com/v1/digests?limit=6",
    );
  });

  it("passes the opaque continuation cursor unchanged", async () => {
    const requestedUrls: string[] = [];
    const fetchMock: FetchDigests = vi.fn(async (input) => {
      requestedUrls.push(String(input));
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
    });

    await fetchDigestsPage({
      apiBaseUrl: "https://api.example.com/",
      cursor: "payload.signature",
      fetchImpl: fetchMock,
    });

    expect(new URL(requestedUrls[0] ?? "").searchParams.get("cursor")).toBe("payload.signature");
  });

  it.each([
    { ...digest, status: "draft" },
    { ...digest, digest_date: "2026-02-30" },
    { title: "Missing required fields" },
  ])("rejects an invalid or unpublished digest payload", async (invalidDigest) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      items: [invalidDigest],
      next_cursor: null,
    }), { status: 200 }));

    await expect(fetchDigestsPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toThrow(DigestsApiError);
  });

  it("does not expose an unsuccessful response body", async () => {
    const fetchMock = vi.fn(async () => new Response("private database detail", { status: 503 }));

    await expect(fetchDigestsPage({
      apiBaseUrl: "https://api.example.com",
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toMatchObject({
      status: 503,
      message: "The digest service returned an error. Please try again.",
    });
  });
});

describe("digest feed helpers", () => {
  it("merges cursor pages without duplicate IDs", () => {
    const second = { ...digest, id: "01a034ed-e100-7e73-ab06-1fecafdc495d" };
    expect(mergeDigests([digest], [digest, second, second])).toEqual([digest, second]);
  });
});
