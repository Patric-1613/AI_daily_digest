import type { DigestSummary } from "./api/digests";

interface DigestFeedProps {
  digests: readonly DigestSummary[];
  initialLoading: boolean;
  loadingMore: boolean;
  error: string | null;
  nextCursor: string | null;
  onRetry: () => void;
  onLoadMore: () => void;
}

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

function formatDigestDate(digestDate: string): string {
  return dateFormatter.format(new Date(`${digestDate}T00:00:00Z`));
}

export function DigestFeed({
  digests,
  initialLoading,
  loadingMore,
  error,
  nextCursor,
  onRetry,
  onLoadMore,
}: DigestFeedProps) {
  return (
    <section className="digestFeed" aria-labelledby="digests-heading" aria-busy={initialLoading || loadingMore}>
      <div className="feedHeading">
        <div>
          <p className="sectionLabel">Published editions</p>
          <h2 id="digests-heading">Daily digests</h2>
        </div>
        {digests.length > 0 ? <span>{digests.length} loaded</span> : null}
      </div>

      {initialLoading ? (
        <div className="feedState" role="status">
          <span className="loadingMark" aria-hidden="true" />
          <div><h3>Loading published digests</h3><p>Looking for the latest evidence-checked editions…</p></div>
        </div>
      ) : null}

      {!initialLoading && error && digests.length === 0 ? (
        <div className="feedState feedStateError" role="alert">
          <div><h3>Digests are temporarily unavailable</h3><p>{error}</p></div>
          <button type="button" onClick={onRetry}>Try again</button>
        </div>
      ) : null}

      {!initialLoading && !error && digests.length === 0 ? (
        <div className="feedState" role="status">
          <div><h3>No published digests yet</h3><p>The first edition will appear after its claims pass publication checks.</p></div>
        </div>
      ) : null}

      {digests.length > 0 ? (
        <div className="digestList">
          {digests.map((digest) => (
            <article className="digestCard" key={digest.id}>
              <div className="digestDate">
                <time dateTime={digest.digest_date}>{formatDigestDate(digest.digest_date)}</time>
                <span>Published</span>
              </div>
              <h3>{digest.title}</h3>
              <p>Evidence-checked edition</p>
            </article>
          ))}
        </div>
      ) : null}

      {error && digests.length > 0 ? (
        <div className="inlineError" role="alert">
          <span>{error}</span><button type="button" onClick={onLoadMore}>Try again</button>
        </div>
      ) : null}

      {nextCursor && !error ? (
        <button className="loadMoreButton" type="button" onClick={onLoadMore} disabled={loadingMore}>
          {loadingMore ? "Loading more…" : "Load more digests"}
        </button>
      ) : null}
    </section>
  );
}
