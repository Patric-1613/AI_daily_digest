import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { fetchUpdatesPage, mergeUpdates, UpdatesApiError } from "./api/updates";
import type { UpdateSummary } from "./api/updates";
import { publicConfig } from "./config";
import { UpdatesFeed } from "./UpdatesFeed";

const models = [
  { name: "Claude", icon: "✦", colors: ["#7B5CFF", "#B45CFF"] },
  { name: "GPT-4", icon: "◎", colors: ["#00D9E8", "#1473E6"] },
  { name: "Gemini", icon: "✧", colors: ["#5B8CFF", "#B65CFF"] },
  { name: "DeepSeek", icon: "◈", colors: ["#22B7E8", "#4055D8"] },
  { name: "Llama", icon: "∞", colors: ["#5675FF", "#7B5CFF"] },
  { name: "Grok", icon: "𝕏", colors: ["#F58025", "#D84A5C"] },
];

export default function App() {
  const [updates, setUpdates] = useState<UpdateSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const retryInitialUpdates = useCallback(async () => {
    setInitialLoading(true);
    setError(null);
    try {
      const page = await fetchUpdatesPage({ apiBaseUrl: publicConfig.apiBaseUrl });
      setUpdates(page.items);
      setNextCursor(page.next_cursor);
    } catch (loadError) {
      const message = loadError instanceof UpdatesApiError
        ? loadError.message
        : "The updates service could not be reached.";
      setError(message);
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchUpdatesPage({
      apiBaseUrl: publicConfig.apiBaseUrl,
      signal: controller.signal,
    }).then((page) => {
      setUpdates(page.items);
      setNextCursor(page.next_cursor);
      setError(null);
    }).catch((loadError: unknown) => {
      if (loadError instanceof DOMException && loadError.name === "AbortError") return;
      const message = loadError instanceof UpdatesApiError
        ? loadError.message
        : "The updates service could not be reached.";
      setError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setInitialLoading(false);
    });
    return () => controller.abort();
  }, []);

  const loadMoreUpdates = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await fetchUpdatesPage({
        apiBaseUrl: publicConfig.apiBaseUrl,
        cursor: nextCursor,
      });
      setUpdates((current) => mergeUpdates(current, page.items));
      setNextCursor(page.next_cursor);
    } catch (loadError) {
      const message = loadError instanceof UpdatesApiError
        ? loadError.message
        : "The updates service could not be reached.";
      setError(message);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, nextCursor]);

  return (
    <main>
      <header className="siteHeader">
        <a className="brand" href="#top" aria-label="AI Daily Digest home">AI Daily Digest</a>
        <span className="today">Live source feed</span>
        <div className="headerActions">
          <label className="srOnly" htmlFor="digest-period">Browse past digests</label>
          <select id="digest-period" defaultValue="today">
            <option value="today">Today</option>
            <option value="yesterday">Yesterday</option>
            <option value="week">This week</option>
          </select>
          <div className="subscribeField">
            <label className="srOnly" htmlFor="email">Email address</label>
            <input id="email" type="email" placeholder="you@example.com" />
            <button type="button">Subscribe</button>
          </div>
        </div>
      </header>

      <div className="pageShell" id="top">
        <section className="hero" aria-labelledby="digest-heading">
          <p className="eyebrow">Source-backed AI industry monitoring</p>
          <h1 id="digest-heading">The signal in AI,<span> without the noise.</span></h1>
          <p className="heroCopy">A concise, source-aware digest of the research, policy and products shaping artificial intelligence today.</p>
          <div className="heroMeta" aria-label="Digest statistics">
            <span>{updates.length} updates loaded</span><span>Official sources</span><span>Cursor-paginated</span>
          </div>
        </section>

        <section className="modelSection" aria-labelledby="models-heading">
          <div className="sectionIntro">
            <div><p className="sectionLabel">Illustrative model explorer · not API-backed</p><h2 id="models-heading">Follow the systems making news</h2></div>
            <span className="railHint">Scroll to explore →</span>
          </div>
          <div className="modelRail">
            {models.map((model, index) => {
              const tileStyle = {
                "--model-start": model.colors[0],
                "--model-end": model.colors[1],
                "--float-delay": `${index * -0.45}s`,
              } as CSSProperties;
              return (
                <button className="modelTile" key={model.name} style={tileStyle} type="button" aria-label={`Explore ${model.name} updates`}>
                  <span className="modelIcon" aria-hidden="true">{model.icon}</span><span>{model.name}</span>
                </button>
              );
            })}
          </div>
        </section>

        <div className="contentGrid">
          <UpdatesFeed
            updates={updates}
            initialLoading={initialLoading}
            loadingMore={loadingMore}
            error={error}
            nextCursor={nextCursor}
            onRetry={() => void retryInitialUpdates()}
            onLoadMore={() => void loadMoreUpdates()}
          />

          <aside className="chatCard" aria-labelledby="chat-heading">
            <span className="statusDot" aria-hidden="true" /><p className="sectionLabel">Illustrative assistant preview · not API-backed</p><h2 id="chat-heading">Ask about today&apos;s digest.</h2>
            <p className="chatIntro">Get a quick answer grounded in the stories and sources collected for this edition.</p>
            <div className="quickReplies"><button type="button">What&apos;s new in AI regulation?</button><button type="button">Which breakthroughs matter?</button><button type="button">Recent funding rounds</button></div>
            <div className="sampleReply"><span>AI</span><p>Today&apos;s strongest research theme is verifiable reasoning—labs are prioritising traceability alongside raw performance.</p></div>
            <div className="askField"><input aria-label="Ask a question" placeholder="Ask a question…" /><button type="button" aria-label="Send question">↑</button></div>
          </aside>
        </div>
      </div>

      <footer className="trustStrip"><p><strong>Illustrative trust metrics</strong><span>40 claims checked</span><span>38 sourced</span><span className="pending">2 pending</span></p><a href="#method">How this works →</a></footer>
    </main>
  );
}
