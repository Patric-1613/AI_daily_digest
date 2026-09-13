import { useCallback, useEffect, useRef, useState } from "react";
import { DigestFeed } from "./DigestFeed";
import {
  DigestsApiError,
  fetchDigestDetail,
  fetchDigestsPage,
} from "./api/digests";
import type { DigestDetail, DigestSummary } from "./api/digests";
import { fetchUpdatesPage, UpdatesApiError } from "./api/updates";
import type { UpdateSummary } from "./api/updates";
import { publicConfig } from "./config";
import { UpdatesFeed } from "./UpdatesFeed";
import { SubscribeForm } from "./Subscriptions";
import { FIRST_PAGE, getKnownTerminalPage, hasNextFrom, maxKnownPage } from "./pagination";
import type { PageCache } from "./pagination";

export type DigestPeriod = "all" | "today" | "yesterday" | "week" | "month" | "year";

export function getPeriodDateRange(
  period: DigestPeriod,
  now: Date = new Date(),
): { date_from: string | null; date_to: string | null } {
  const formatUtcDate = (d: Date): string => d.toISOString().slice(0, 10);
  const todayUtc = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));

  if (period === "all") {
    return { date_from: null, date_to: null };
  }
  if (period === "today") {
    const tomorrow = new Date(todayUtc);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    return { date_from: formatUtcDate(todayUtc), date_to: formatUtcDate(tomorrow) };
  }
  if (period === "yesterday") {
    const yesterday = new Date(todayUtc);
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    return { date_from: formatUtcDate(yesterday), date_to: formatUtcDate(todayUtc) };
  }
  if (period === "week") {
    const weekAgo = new Date(todayUtc);
    weekAgo.setUTCDate(weekAgo.getUTCDate() - 7);
    return { date_from: formatUtcDate(weekAgo), date_to: null };
  }
  if (period === "month") {
    const monthAgo = new Date(todayUtc);
    monthAgo.setUTCDate(monthAgo.getUTCDate() - 30);
    return { date_from: formatUtcDate(monthAgo), date_to: null };
  }
  if (period === "year") {
    const yearAgo = new Date(todayUtc);
    yearAgo.setUTCDate(yearAgo.getUTCDate() - 365);
    return { date_from: formatUtcDate(yearAgo), date_to: null };
  }
  return { date_from: null, date_to: null };
}

type AppProps = {
  subscriptionsEnabled?: boolean;
};

export default function App({
  subscriptionsEnabled = publicConfig.subscriptionsEnabled,
}: AppProps) {
  const [digestPeriod, setDigestPeriod] = useState<DigestPeriod>("all");
  const [digestPages, setDigestPages] = useState<PageCache<DigestSummary>>(new Map());
  const [digestCurrentPage, setDigestCurrentPage] = useState(FIRST_PAGE);
  const [digestsInitialLoading, setDigestsInitialLoading] = useState(true);
  const [digestsLoadingMore, setDigestsLoadingMore] = useState(false);
  const [digestsError, setDigestsError] = useState<string | null>(null);
  const [digestRequestVersion, setDigestRequestVersion] = useState(0);
  const digestLoadMoreController = useRef<AbortController | null>(null);
  const [selectedDigestId, setSelectedDigestId] = useState<string | null>(null);
  const [digestDetail, setDigestDetail] = useState<DigestDetail | null>(null);
  const [digestDetailLoading, setDigestDetailLoading] = useState(false);
  const [digestDetailError, setDigestDetailError] = useState<string | null>(null);
  const [digestDetailRequestVersion, setDigestDetailRequestVersion] = useState(0);
  const digestDetailController = useRef<AbortController | null>(null);
  const [updatePages, setUpdatePages] = useState<PageCache<UpdateSummary>>(new Map());
  const [updateCurrentPage, setUpdateCurrentPage] = useState(FIRST_PAGE);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [initialRequestVersion, setInitialRequestVersion] = useState(0);
  const loadMoreController = useRef<AbortController | null>(null);

  const digests = digestPages.get(digestCurrentPage)?.items ?? [];
  const digestHasNext = hasNextFrom(digestPages, digestCurrentPage);
  const digestHighestCachedPage = maxKnownPage(digestPages);
  const digestTerminalPage = getKnownTerminalPage(digestPages);
  const updates = updatePages.get(updateCurrentPage)?.items ?? [];
  const updatesHasNext = hasNextFrom(updatePages, updateCurrentPage);
  const updatesHighestCachedPage = maxKnownPage(updatePages);
  const updatesTerminalPage = getKnownTerminalPage(updatePages);

  const retryInitialUpdates = useCallback(() => {
    setInitialLoading(true);
    setError(null);
    setInitialRequestVersion((version) => version + 1);
  }, []);

  const retryInitialDigests = useCallback(() => {
    setDigestsInitialLoading(true);
    setDigestsError(null);
    setDigestRequestVersion((version) => version + 1);
  }, []);

  const closeDigestDetail = useCallback(() => {
    digestDetailController.current?.abort();
    setSelectedDigestId(null);
    setDigestDetail(null);
    setDigestDetailError(null);
    setDigestDetailLoading(false);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { date_from, date_to } = getPeriodDateRange(digestPeriod);
    void fetchDigestsPage({
      apiBaseUrl: publicConfig.apiBaseUrl,
      date_from,
      date_to,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setDigestPages(new Map([[FIRST_PAGE, { items: page.items, nextCursor: page.next_cursor }]]));
      setDigestCurrentPage(FIRST_PAGE);
      setDigestsError(null);
    }).catch((loadError: unknown) => {
      if (controller.signal.aborted) return;
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof DigestsApiError
        ? loadError.message
        : "The digest service could not be reached.";
      setDigestsError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setDigestsInitialLoading(false);
    });
    return () => {
      controller.abort();
      digestLoadMoreController.current?.abort();
    };
    // digestRequestVersion is a manual retry trigger; digestPeriod changes reset the cache
    // synchronously below, before this effect ever fetches the filtered period's page one.
  }, [digestPeriod, digestRequestVersion]);

  useEffect(() => {
    if (selectedDigestId === null) return undefined;
    const controller = new AbortController();
    digestDetailController.current = controller;
    void fetchDigestDetail({
      apiBaseUrl: publicConfig.apiBaseUrl,
      digestId: selectedDigestId,
      signal: controller.signal,
    }).then((loadedDetail) => {
      if (controller.signal.aborted) return;
      setDigestDetail(loadedDetail);
      setDigestDetailError(null);
    }).catch((loadError: unknown) => {
      if (controller.signal.aborted) return;
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof DigestsApiError
        ? loadError.message
        : "The digest details could not be reached.";
      setDigestDetailError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setDigestDetailLoading(false);
    });
    return () => controller.abort();
  }, [digestDetailRequestVersion, selectedDigestId]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchUpdatesPage({
      apiBaseUrl: publicConfig.apiBaseUrl,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setUpdatePages(new Map([[FIRST_PAGE, { items: page.items, nextCursor: page.next_cursor }]]));
      setUpdateCurrentPage(FIRST_PAGE);
      setError(null);
    }).catch((loadError: unknown) => {
      if (controller.signal.aborted) return;
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof UpdatesApiError
        ? loadError.message
        : "The updates service could not be reached.";
      setError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setInitialLoading(false);
    });
    return () => {
      controller.abort();
      loadMoreController.current?.abort();
    };
  }, [initialRequestVersion]);

  const goToNextUpdates = useCallback(async () => {
    if (loadingMore) return;
    const targetPage = updateCurrentPage + 1;
    if (updatePages.has(targetPage)) {
      setUpdateCurrentPage(targetPage);
      return;
    }
    const cursor = updatePages.get(updateCurrentPage)?.nextCursor ?? null;
    if (!cursor) return;
    loadMoreController.current?.abort();
    const controller = new AbortController();
    loadMoreController.current = controller;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await fetchUpdatesPage({
        apiBaseUrl: publicConfig.apiBaseUrl,
        cursor,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setUpdatePages((current) => {
        const next = new Map(current);
        next.set(targetPage, { items: page.items, nextCursor: page.next_cursor });
        return next;
      });
      setUpdateCurrentPage(targetPage);
    } catch (loadError) {
      if (controller.signal.aborted) return;
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof UpdatesApiError
        ? loadError.message
        : "The updates service could not be reached.";
      setError(message);
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }, [loadingMore, updateCurrentPage, updatePages]);

  const goToPreviousUpdates = useCallback(() => {
    setUpdateCurrentPage((page) => Math.max(FIRST_PAGE, page - 1));
  }, []);

  const goToUpdatesPage = useCallback((page: number) => {
    if (updatePages.has(page)) setUpdateCurrentPage(page);
  }, [updatePages]);

  const goToNextDigests = useCallback(async () => {
    if (digestsLoadingMore) return;
    const targetPage = digestCurrentPage + 1;
    closeDigestDetail();
    if (digestPages.has(targetPage)) {
      setDigestCurrentPage(targetPage);
      return;
    }
    const cursor = digestPages.get(digestCurrentPage)?.nextCursor ?? null;
    if (!cursor) return;
    digestLoadMoreController.current?.abort();
    const controller = new AbortController();
    digestLoadMoreController.current = controller;
    setDigestsLoadingMore(true);
    setDigestsError(null);
    const { date_from, date_to } = getPeriodDateRange(digestPeriod);
    try {
      const page = await fetchDigestsPage({
        apiBaseUrl: publicConfig.apiBaseUrl,
        cursor,
        date_from,
        date_to,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setDigestPages((current) => {
        const next = new Map(current);
        next.set(targetPage, { items: page.items, nextCursor: page.next_cursor });
        return next;
      });
      setDigestCurrentPage(targetPage);
    } catch (loadError) {
      if (controller.signal.aborted) return;
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof DigestsApiError
        ? loadError.message
        : "The digest service could not be reached.";
      setDigestsError(message);
    } finally {
      if (!controller.signal.aborted) setDigestsLoadingMore(false);
    }
  }, [closeDigestDetail, digestCurrentPage, digestPages, digestPeriod, digestsLoadingMore]);

  const goToPreviousDigests = useCallback(() => {
    closeDigestDetail();
    setDigestCurrentPage((page) => Math.max(FIRST_PAGE, page - 1));
  }, [closeDigestDetail]);

  const goToDigestsPage = useCallback((page: number) => {
    closeDigestDetail();
    if (digestPages.has(page)) setDigestCurrentPage(page);
  }, [closeDigestDetail, digestPages]);

  const selectDigest = useCallback((digestId: string) => {
    digestDetailController.current?.abort();
    if (digestId === selectedDigestId) {
      setSelectedDigestId(null);
      setDigestDetail(null);
      setDigestDetailError(null);
      setDigestDetailLoading(false);
      return;
    }
    setSelectedDigestId(digestId);
    setDigestDetail(null);
    setDigestDetailError(null);
    setDigestDetailLoading(true);
  }, [selectedDigestId]);

  const retryDigestDetail = useCallback(() => {
    digestDetailController.current?.abort();
    setDigestDetail(null);
    setDigestDetailError(null);
    setDigestDetailLoading(true);
    setDigestDetailRequestVersion((version) => version + 1);
  }, []);

  return (
    <main>
      <header className="siteHeader">
        <a className="brand" href="#top" aria-label="AI Daily Digest home">AI Daily Digest</a>
        <span className="today">Live source feed</span>
        <div className="headerActions">
          <label className="srOnly" htmlFor="digest-period">Browse past digests</label>
          <select
            id="digest-period"
            value={digestPeriod}
            onChange={(e) => {
              const newPeriod = e.target.value as DigestPeriod;
              digestLoadMoreController.current?.abort();
              digestDetailController.current?.abort();
              setDigestPeriod(newPeriod);
              setDigestPages(new Map());
              setDigestCurrentPage(FIRST_PAGE);
              setDigestsLoadingMore(false);
              setSelectedDigestId(null);
              setDigestDetail(null);
              setDigestDetailError(null);
              setDigestDetailLoading(false);
              setDigestsInitialLoading(true);
              setDigestsError(null);
            }}
          >
            <option value="all">All editions</option>
            <option value="today">Today</option>
            <option value="yesterday">Yesterday</option>
            <option value="week">This week</option>
            <option value="month">This month</option>
            <option value="year">This year</option>
          </select>
          {subscriptionsEnabled ? <SubscribeForm /> : null}
        </div>
      </header>

      <div className="pageShell" id="top">
        <section className="hero" aria-labelledby="digest-heading">
          <p className="eyebrow">Source-backed AI industry monitoring</p>
          <h1 id="digest-heading">The signal in AI,<span> without the noise.</span></h1>
          <p className="heroCopy">A concise, source-aware digest of the research, policy and products shaping artificial intelligence today.</p>
          <div className="heroMeta" aria-label="Digest statistics">
            <span>{digests.length} digests loaded</span><span>{updates.length} updates loaded</span><span>Official sources</span>
          </div>
        </section>

        <div className="contentGrid">
          <div className="feedsColumn">
            <DigestFeed
              digests={digests}
              initialLoading={digestsInitialLoading}
              loadingMore={digestsLoadingMore}
              error={digestsError}
              currentPage={digestCurrentPage}
              highestCachedPage={digestHighestCachedPage}
              terminalPage={digestTerminalPage}
              hasNext={digestHasNext}
              selectedDigestId={selectedDigestId}
              detail={digestDetail}
              detailLoading={digestDetailLoading}
              detailError={digestDetailError}
              onRetry={() => void retryInitialDigests()}
              onGoToPage={goToDigestsPage}
              onPrevious={goToPreviousDigests}
              onNext={() => void goToNextDigests()}
              onSelectDigest={selectDigest}
              onCloseDetail={closeDigestDetail}
              onRetryDetail={retryDigestDetail}
            />
            <UpdatesFeed
              updates={updates}
              initialLoading={initialLoading}
              loadingMore={loadingMore}
              error={error}
              currentPage={updateCurrentPage}
              highestCachedPage={updatesHighestCachedPage}
              terminalPage={updatesTerminalPage}
              hasNext={updatesHasNext}
              onRetry={() => void retryInitialUpdates()}
              onGoToPage={goToUpdatesPage}
              onPrevious={goToPreviousUpdates}
              onNext={() => void goToNextUpdates()}
            />
          </div>

        </div>
      </div>
    </main>
  );
}
