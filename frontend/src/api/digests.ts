export interface DigestSummary {
  id: string;
  digest_date: string;
  status: "published";
  title: string;
}

export interface DigestsPage {
  items: DigestSummary[];
  next_cursor: string | null;
}

export type FetchDigests = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export class DigestsApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "DigestsApiError";
    this.status = status;
  }
}

interface FetchDigestsPageOptions {
  apiBaseUrl: string;
  cursor?: string | null;
  signal?: AbortSignal;
  fetchImpl?: FetchDigests;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return value;
}

function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function parseDigest(value: unknown): DigestSummary {
  if (!isRecord(value)) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }

  const digestDate = requiredString(value, "digest_date");
  if (!isCalendarDate(digestDate) || value.status !== "published") {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }

  return {
    id: requiredString(value, "id"),
    digest_date: digestDate,
    status: "published",
    title: requiredString(value, "title"),
  };
}

function parsePage(value: unknown): DigestsPage {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  const nextCursor = value.next_cursor;
  if (nextCursor !== null && typeof nextCursor !== "string") {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return {
    items: value.items.map(parseDigest),
    next_cursor: nextCursor,
  };
}

export async function fetchDigestsPage({
  apiBaseUrl,
  cursor,
  signal,
  fetchImpl = fetch,
}: FetchDigestsPageOptions): Promise<DigestsPage> {
  let url: URL;
  try {
    const normalizedBaseUrl = apiBaseUrl.endsWith("/") ? apiBaseUrl : `${apiBaseUrl}/`;
    url = new URL("v1/digests", normalizedBaseUrl);
  } catch {
    throw new DigestsApiError("The configured API address is invalid.");
  }

  url.searchParams.set("limit", "6");
  if (cursor) url.searchParams.set("cursor", cursor);

  let response: Response;
  try {
    response = await fetchImpl(url, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new DigestsApiError("The digest service could not be reached.");
  }

  if (!response.ok) {
    throw new DigestsApiError(
      "The digest service returned an error. Please try again.",
      response.status,
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return parsePage(payload);
}

export function mergeDigests(
  current: readonly DigestSummary[],
  incoming: readonly DigestSummary[],
): DigestSummary[] {
  const seen = new Set(current.map((digest) => digest.id));
  const merged = [...current];
  for (const digest of incoming) {
    if (seen.has(digest.id)) continue;
    seen.add(digest.id);
    merged.push(digest);
  }
  return merged;
}
