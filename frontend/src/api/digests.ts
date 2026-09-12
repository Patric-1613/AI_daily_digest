export interface DigestSummary {
  id: string;
  digest_date: string;
  status: "published";
  title: string;
}

// "unsupported" is defensive: published detail should currently return supported claims only.
export type DigestClaimValidationStatus = "supported" | "unsupported";

export interface DigestCitation {
  snapshot_id: string;
  canonical_url: string;
  source_title: string;
}

export interface DigestClaim {
  id: string;
  text: string;
  validation_status: DigestClaimValidationStatus;
  citation_snapshot_ids: string[];
  citations: DigestCitation[];
}

export interface DigestDetail extends DigestSummary {
  claims: DigestClaim[];
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

interface FetchDigestDetailOptions {
  apiBaseUrl: string;
  digestId: string;
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

function optionalStringArray(record: Record<string, unknown>, key: string): string[] {
  const value = record[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function parseCitation(value: unknown): DigestCitation | null {
  if (!isRecord(value)) return null;
  const canonicalUrl = safeHttpUrl(value.canonical_url);
  const snapshotId = value.snapshot_id;
  const sourceTitle = value.source_title;
  if (
    canonicalUrl === null
    || typeof snapshotId !== "string"
    || snapshotId.length === 0
    || typeof sourceTitle !== "string"
    || sourceTitle.trim().length === 0
  ) {
    return null;
  }
  return {
    snapshot_id: snapshotId,
    canonical_url: canonicalUrl,
    source_title: sourceTitle.trim(),
  };
}

function parseClaim(value: unknown): DigestClaim {
  if (!isRecord(value)) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  if (value.validation_status !== "supported" && value.validation_status !== "unsupported") {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  const rawCitations = Array.isArray(value.citations) ? value.citations : [];
  const citations = rawCitations
    .map(parseCitation)
    .filter((citation): citation is DigestCitation => citation !== null);
  if (citations.length === 0) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return {
    id: requiredString(value, "id"),
    text: requiredString(value, "text"),
    validation_status: value.validation_status,
    citation_snapshot_ids: optionalStringArray(value, "citation_snapshot_ids"),
    citations,
  };
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

function parseDetail(value: unknown): DigestDetail {
  const summary = parseDigest(value);
  if (!isRecord(value) || !Array.isArray(value.claims)) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return {
    ...summary,
    claims: value.claims.map(parseClaim),
  };
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
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
    if (isAbortError(error)) throw error;
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

export async function fetchDigestDetail({
  apiBaseUrl,
  digestId,
  signal,
  fetchImpl = fetch,
}: FetchDigestDetailOptions): Promise<DigestDetail> {
  if (!digestId) throw new DigestsApiError("The digest link is invalid.", 422);

  let url: URL;
  try {
    const normalizedBaseUrl = apiBaseUrl.endsWith("/") ? apiBaseUrl : `${apiBaseUrl}/`;
    url = new URL(`v1/digests/${encodeURIComponent(digestId)}`, normalizedBaseUrl);
  } catch {
    throw new DigestsApiError("The configured API address is invalid.");
  }

  let response: Response;
  try {
    response = await fetchImpl(url, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new DigestsApiError("The digest details could not be reached.");
  }

  if (!response.ok) {
    const message = response.status === 404
      ? "This published digest is no longer available."
      : response.status === 422
        ? "The digest link is invalid."
        : "The digest details could not be loaded. Please try again.";
    throw new DigestsApiError(message, response.status);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }

  const detail = parseDetail(payload);
  if (detail.id !== digestId) {
    throw new DigestsApiError("The digest service returned an invalid response.");
  }
  return detail;
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
