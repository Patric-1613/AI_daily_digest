import { describe, expect, it, vi } from "vitest";
import {
  DigestsApiError,
  fetchDigestDetail,
  fetchDigestsPage,
  mergeDigests,
} from "./digests";
import type { DigestDetail, DigestSummary, FetchDigests } from "./digests";

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
    citation_snapshot_ids: ["01a032cd-23e0-76d3-a27c-f608ccc02226"],
    citations: [{
      snapshot_id: "01a032cd-23e0-76d3-a27c-f608ccc02226",
      canonical_url: "https://www.anthropic.com/news/claude-2-1",
      source_title: "Introducing Claude 2.1",
    }],
  }],
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

describe("digest detail API client", () => {
  it("requests and maps the published detail contract", async () => {
    const requestedUrls: string[] = [];
    const fetchMock: FetchDigests = vi.fn(async (input) => {
      requestedUrls.push(String(input));
      return new Response(JSON.stringify(detail), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const result = await fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock,
    });

    expect(result).toEqual(detail);
    expect(requestedUrls).toEqual([`https://api.example.com/v1/digests/${digest.id}`]);
  });

  it("rejects a javascript citation when filtering leaves no usable citation", async () => {
    const unsafePayload = {
      ...detail,
      claims: [{
        ...detail.claims[0],
        citations: [{
          snapshot_id: "unsafe",
          canonical_url: "javascript:alert(1)",
          source_title: "Unsafe source",
        }],
      }],
    };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(unsafePayload), { status: 200 }));

    await expect(fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toThrow(DigestsApiError);
  });

  it.each([
    ["an empty source title", [{
      snapshot_id: "empty-title",
      canonical_url: "https://example.com/source",
      source_title: "",
    }]],
    ["a non-HTTP URL", [{
      snapshot_id: "non-http",
      canonical_url: "ftp://example.com/source",
      source_title: "Unsupported protocol",
    }]],
    ["a missing citations field", undefined],
  ])("rejects a claim with %s", async (_caseName, citations) => {
    const invalidPayload = {
      ...detail,
      claims: [{ ...detail.claims[0], citations }],
    };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(invalidPayload), {
      status: 200,
    }));

    await expect(fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toThrow(DigestsApiError);
  });

  it("keeps safe HTTP links when filtering malformed citations", async () => {
    const payload = {
      ...detail,
      claims: [{
        ...detail.claims[0],
        citations: [
          ...detail.claims[0]!.citations,
          {
            snapshot_id: "unsafe",
            canonical_url: "javascript:alert(1)",
            source_title: "Unsafe source",
          },
          {
            snapshot_id: "missing-title",
            canonical_url: "https://example.com/source",
            source_title: "",
          },
        ],
      }],
    };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }));

    const result = await fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    });

    expect(result.claims[0]?.citations).toEqual(detail.claims[0]?.citations);
  });

  it.each([
    [404, "This published digest is no longer available."],
    [422, "The digest link is invalid."],
    [503, "The digest details could not be loaded. Please try again."],
  ])("maps HTTP %s to a safe message", async (status, message) => {
    const fetchMock = vi.fn(async () => new Response("private server detail", { status }));

    await expect(fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toMatchObject({ status, message });
  });

  it("rejects a mismatched or unpublished detail payload", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      ...detail,
      id: "01a034ed-e100-7e73-ab06-1fecafdc495d",
    }), { status: 200 }));

    await expect(fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toThrow(DigestsApiError);
  });

  it("passes through AbortError so stale-request owners can ignore it", async () => {
    const aborted = new DOMException("stale", "AbortError");
    const fetchMock = vi.fn(async () => { throw aborted; });

    await expect(fetchDigestDetail({
      apiBaseUrl: "https://api.example.com",
      digestId: digest.id,
      fetchImpl: fetchMock as FetchDigests,
    })).rejects.toBe(aborted);
  });
});

describe("digest feed helpers", () => {
  it("merges cursor pages without duplicate IDs", () => {
    const second = { ...digest, id: "01a034ed-e100-7e73-ab06-1fecafdc495d" };
    expect(mergeDigests([digest], [digest, second, second])).toEqual([digest, second]);
  });
});
