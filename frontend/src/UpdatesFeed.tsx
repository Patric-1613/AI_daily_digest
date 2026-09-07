import type { UpdateSummary } from "./api/updates";
import { safeSourceUrl } from "./api/updates";

interface UpdatesFeedProps {
  updates: readonly UpdateSummary[];
  initialLoading: boolean;
  loadingMore: boolean;
  error: string | null;
  nextCursor: string | null;
  onRetry: () => void;
  onLoadMore: () => void;
}

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});

function formatTimestamp(rawTimestamp: string): string {
  const parsed = new Date(rawTimestamp);
  return Number.isNaN(parsed.getTime()) ? "Time unavailable" : dateFormatter.format(parsed);
}

export function UpdatesFeed({
  updates,
  initialLoading,
  loadingMore,
  error,
  nextCursor,
  onRetry,
  onLoadMore,
}: UpdatesFeedProps) {
  return (
    <section className="feed" aria-labelledby="updates-heading" aria-busy={initialLoading || loadingMore}>
      <div className="feedHeading">
        <div>
          <p className="sectionLabel">Official source feed</p>
          <h2 id="updates-heading">Latest updates</h2>
        </div>
        {updates.length > 0 ? <span>{updates.length} loaded</span> : null}
      </div>

      {initialLoading ? (
        <div className="feedState" role="status">
          <span className="loadingMark" aria-hidden="true" />
          <div><h3>Loading source updates</h3><p>Connecting to the AI Daily Digest API…</p></div>
        </div>
      ) : null}

      {!initialLoading && error && updates.length === 0 ? (
        <div className="feedState feedStateError" role="alert">
          <div><h3>Updates are temporarily unavailable</h3><p>{error}</p></div>
          <button type="button" onClick={onRetry}>Try again</button>
        </div>
      ) : null}

      {!initialLoading && !error && updates.length === 0 ? (
        <div className="feedState" role="status">
          <div><h3>No updates yet</h3><p>The collector has not published any source updates.</p></div>
        </div>
      ) : null}

      {updates.length > 0 ? (
        <div className="articleList">
          {updates.map((update) => {
            const sourceUrl = safeSourceUrl(update.canonical_url);
            const timestamp = update.published_at ?? update.first_fetched_at;
            const timestampLabel = update.published_at ? "Published" : "First collected";
            return (
              <article className="articleCard" key={update.id}>
                <div>
                  <div className="articleMeta">
                    <span>{update.publisher}</span>
                    <time dateTime={timestamp}>{timestampLabel} {formatTimestamp(timestamp)}</time>
                  </div>
                  <h3>{update.title}</h3>
                  {update.tags.length > 0 ? (
                    <ul className="tagList" aria-label="Update topics">
                      {update.tags.map((tag, index) => <li key={`${tag}-${index}`}>{tag.replaceAll("_", " ")}</li>)}
                    </ul>
                  ) : <p className="sourceIdentifier">Source: {update.source_id}</p>}
                </div>
                {sourceUrl ? (
                  <a href={sourceUrl} target="_blank" rel="noreferrer noopener">
                    Read at {update.publisher} →
                  </a>
                ) : <span className="unavailableLink">Source link unavailable</span>}
              </article>
            );
          })}
        </div>
      ) : null}

      {error && updates.length > 0 ? (
        <div className="inlineError" role="alert">
          <span>{error}</span><button type="button" onClick={onLoadMore}>Try again</button>
        </div>
      ) : null}

      {nextCursor && !error ? (
        <button className="loadMoreButton" type="button" onClick={onLoadMore} disabled={loadingMore}>
          {loadingMore ? "Loading more…" : "Load more updates"}
        </button>
      ) : null}
    </section>
  );
}
