// Filters the Latest updates feed only -- see App.tsx's own comment on
// `selectedSourceId` for why this is `source_id`, not `publisher`
// (LangChain and LangGraph are both published through PyPI, so filtering by
// publisher could not tell their entries apart).

export interface SourceFilterOption {
  /** `null` means "All sources" -- no `source_id` query parameter at all. */
  sourceId: string | null;
  label: string;
  /** Short, local, typographic mark -- never a copied or hotlinked logo. */
  mark: string;
  /** CSS custom property suffix, e.g. "openai" -> var(--accent-openai). */
  accent: string;
}

export const SOURCE_FILTER_OPTIONS: readonly SourceFilterOption[] = [
  { sourceId: null, label: "All sources", mark: "ALL", accent: "all" },
  { sourceId: "openai_news", label: "OpenAI", mark: "OAI", accent: "openai" },
  { sourceId: "anthropic_news", label: "Anthropic", mark: "A", accent: "anthropic" },
  { sourceId: "langchain_pypi", label: "LangChain", mark: "LC", accent: "langchain" },
  { sourceId: "langgraph_pypi", label: "LangGraph", mark: "LG", accent: "langgraph" },
];

/** The display label for a `source_id` (or `null` for "All sources"), for
 * building a truthful, provider-named empty state -- never a fabricated count
 * or freshness claim. Falls back to the raw id if it is ever an id this list
 * doesn't know about, rather than silently showing nothing. */
export function labelForSourceId(sourceId: string | null): string {
  return SOURCE_FILTER_OPTIONS.find((option) => option.sourceId === sourceId)?.label ?? sourceId ?? "All sources";
}

interface SourceFilterProps {
  selectedSourceId: string | null;
  onSelect: (sourceId: string | null) => void;
}

export function SourceFilter({ selectedSourceId, onSelect }: SourceFilterProps) {
  return (
    <section className="sourceFilter" aria-labelledby="source-filter-heading">
      <p className="sectionLabel">Latest updates filter</p>
      <h2 id="source-filter-heading">Filter latest updates by source</h2>
      <p className="sourceFilterHelp">
        Choose a source to filter the latest updates below. Published editions remain unchanged.
      </p>
      <div className="sourceFilterGrid" role="group" aria-label="Filter latest updates by source">
        {SOURCE_FILTER_OPTIONS.map((option) => {
          const isActive = option.sourceId === selectedSourceId;
          return (
            <button
              key={option.accent}
              type="button"
              className={`sourceCard sourceCard--${option.accent}${isActive ? " sourceCardActive" : ""}`}
              aria-pressed={isActive}
              onClick={() => onSelect(selectedSourceId === option.sourceId ? null : option.sourceId)}
            >
              <span className="sourceMark" aria-hidden="true">{option.mark}</span>
              <span className="sourceName">{option.label}</span>
              {isActive ? <span className="sourceCheck" aria-hidden="true">✓</span> : null}
            </button>
          );
        })}
      </div>
    </section>
  );
}
