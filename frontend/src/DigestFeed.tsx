import { Fragment } from "react";
import type { DigestDetail, DigestSummary } from "./api/digests";

interface DigestFeedProps {
  digests: readonly DigestSummary[];
  initialLoading: boolean;
  loadingMore: boolean;
  error: string | null;
  nextCursor: string | null;
  selectedDigestId: string | null;
  detail: DigestDetail | null;
  detailLoading: boolean;
  detailError: string | null;
  onRetry: () => void;
  onLoadMore: () => void;
  onSelectDigest: (digestId: string) => void;
  onCloseDetail: () => void;
  onRetryDetail: () => void;
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
  selectedDigestId,
  detail,
  detailLoading,
  detailError,
  onRetry,
  onLoadMore,
  onSelectDigest,
  onCloseDetail,
  onRetryDetail,
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
          {digests.map((digest) => {
            const isSelected = selectedDigestId === digest.id;
            const detailPanelId = `digest-detail-${digest.id}`;
            const titleId = `digest-title-${digest.id}`;
            return (
              <Fragment key={digest.id}>
                <article className={`digestCard${isSelected ? " digestCardSelected" : ""}`}>
                  <div className="digestDate">
                    <time dateTime={digest.digest_date}>{formatDigestDate(digest.digest_date)}</time>
                    <span>Published</span>
                  </div>
                  <h3 id={titleId}>{digest.title}</h3>
                  <p>Evidence-checked edition</p>
                  <button
                    className="digestDetailButton"
                    type="button"
                    aria-controls={detailPanelId}
                    aria-expanded={isSelected}
                    aria-label={`View details for ${digest.title}`}
                    onClick={() => onSelectDigest(digest.id)}
                  >
                    {isSelected ? "Details open" : "View details"}
                  </button>
                </article>

                {isSelected ? (
                  <section
                    className="digestDetailPanel"
                    id={detailPanelId}
                    aria-labelledby={titleId}
                    aria-busy={detailLoading}
                  >
                    <div className="digestDetailHeader">
                      <div>
                        <p className="sectionLabel">Published detail</p>
                        <h3>{digest.title}</h3>
                      </div>
                      <button type="button" onClick={onCloseDetail}>Close details</button>
                    </div>

                    {detailLoading ? (
                      <div className="detailState" role="status">
                        <span className="loadingMark" aria-hidden="true" />
                        <p>Loading claims and citations…</p>
                      </div>
                    ) : null}

                    {!detailLoading && detailError ? (
                      <div className="detailState detailStateError" role="alert">
                        <div><h4>Digest details are unavailable</h4><p>{detailError}</p></div>
                        <button type="button" onClick={onRetryDetail}>Try again</button>
                      </div>
                    ) : null}

                    {!detailLoading && !detailError && detail?.claims.length === 0 ? (
                      <div className="detailState" role="status">
                        <p>No claims are available for this published digest.</p>
                      </div>
                    ) : null}

                    {!detailLoading && !detailError && detail && detail.claims.length > 0 ? (
                      <ol className="digestClaims">
                        {detail.claims.map((claim) => (
                          <li key={claim.id}>
                            <div className="claimHeading">
                              <span>Grounded claim</span>
                              <span className="validationStatus">{claim.validation_status}</span>
                            </div>
                            <p>{claim.text}</p>
                            {claim.citations.length > 0 ? (
                              <ul className="citationList" aria-label="Official sources">
                                {claim.citations.map((citation) => (
                                  <li key={`${claim.id}-${citation.snapshot_id}`}>
                                    <a
                                      href={citation.canonical_url}
                                      target="_blank"
                                      rel="noreferrer noopener"
                                      aria-label={`Read official source: ${citation.source_title}`}
                                    >
                                      {citation.source_title} <span aria-hidden="true">↗</span>
                                    </a>
                                  </li>
                                ))}
                              </ul>
                            ) : (
                              <p className="citationUnavailable">
                                No public source link is available for this claim.
                              </p>
                            )}
                          </li>
                        ))}
                      </ol>
                    ) : null}
                  </section>
                ) : null}
              </Fragment>
            );
          })}
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
