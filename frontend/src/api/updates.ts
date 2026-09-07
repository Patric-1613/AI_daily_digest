export interface UpdateSummary {
  id: string;
  source_id: string;
  publisher: string;
  title: string;
  canonical_url: string;
  published_at: string | null;
  first_fetched_at: string;
  event_id: string | null;
  tags: string[];
  language: string | null;
  latest_snapshot_id: string | null;
}

export interface UpdatesPage {
  items: UpdateSummary[];
  next_cursor: string | null;
}

export type FetchUpdates = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export class UpdatesApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "UpdatesApiError";
    this.status = status;
  }
}

interface FetchUpdatesPageOptions {
  apiBaseUrl: string;
  cursor?: string | null;
  signal?: AbortSignal;
  fetchImpl?: FetchUpdates;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }
  return value;
}

function nullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }
  return value;
}

function parseUpdate(value: unknown): UpdateSummary {
  if (!isRecord(value)) {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }

  const tags = value.tags;
  if (!Array.isArray(tags) || !tags.every((tag) => typeof tag === "string")) {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }

  return {
    id: requiredString(value, "id"),
    source_id: requiredString(value, "source_id"),
    publisher: requiredString(value, "publisher"),
    title: requiredString(value, "title"),
    canonical_url: requiredString(value, "canonical_url"),
    published_at: nullableString(value, "published_at"),
    first_fetched_at: requiredString(value, "first_fetched_at"),
    event_id: nullableString(value, "event_id"),
    tags: [...tags],
    language: nullableString(value, "language"),
    latest_snapshot_id: nullableString(value, "latest_snapshot_id"),
  };
}

function parsePage(value: unknown): UpdatesPage {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }
  const nextCursor = value.next_cursor;
  if (nextCursor !== null && typeof nextCursor !== "string") {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }
  return {
    items: value.items.map(parseUpdate),
    next_cursor: nextCursor,
  };
}

export async function fetchUpdatesPage({
  apiBaseUrl,
  cursor,
  signal,
  fetchImpl = fetch,
}: FetchUpdatesPageOptions): Promise<UpdatesPage> {
  let url: URL;
  try {
    const normalizedBaseUrl = apiBaseUrl.endsWith("/") ? apiBaseUrl : `${apiBaseUrl}/`;
    url = new URL("v1/updates", normalizedBaseUrl);
  } catch {
    throw new UpdatesApiError("The configured API address is invalid.");
  }

  url.searchParams.set("limit", "12");
  if (cursor) {
    url.searchParams.set("cursor", cursor);
  }

  let response: Response;
  try {
    response = await fetchImpl(url, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new UpdatesApiError("The updates service could not be reached.");
  }

  if (!response.ok) {
    throw new UpdatesApiError(
      "The updates service returned an error. Please try again.",
      response.status,
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new UpdatesApiError("The updates service returned an invalid response.");
  }
  return parsePage(payload);
}

export function mergeUpdates(
  current: readonly UpdateSummary[],
  incoming: readonly UpdateSummary[],
): UpdateSummary[] {
  const seen = new Set(current.map((item) => item.id));
  const merged = [...current];
  for (const item of incoming) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(item);
  }
  return merged;
}

export function safeSourceUrl(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
