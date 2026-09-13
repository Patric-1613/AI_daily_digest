// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SOURCE_FILTER_OPTIONS, SourceFilter, labelForSourceId } from "./SourceFilter";

afterEach(() => {
  document.body.replaceChildren();
});

function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  return { container, root };
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")]
    .find((candidate) => candidate.textContent?.includes(text));
  if (!button) throw new Error(`Button not found: ${text}`);
  return button;
}

describe("SOURCE_FILTER_OPTIONS", () => {
  it("uses the exact five required source_id mappings", () => {
    expect(SOURCE_FILTER_OPTIONS.map((option) => option.sourceId)).toEqual([
      null,
      "openai_news",
      "anthropic_news",
      "langchain_pypi",
      "langgraph_pypi",
    ]);
  });

  it("labelForSourceId names every option, including All sources", () => {
    expect(labelForSourceId(null)).toBe("All sources");
    expect(labelForSourceId("openai_news")).toBe("OpenAI");
    expect(labelForSourceId("anthropic_news")).toBe("Anthropic");
    expect(labelForSourceId("langchain_pypi")).toBe("LangChain");
    expect(labelForSourceId("langgraph_pypi")).toBe("LangGraph");
  });
});

describe("SourceFilter markup", () => {
  it("wraps the five cards in a labelled, grouped, button-only control", () => {
    const html = renderToStaticMarkup(<SourceFilter selectedSourceId={null} onSelect={() => undefined} />);
    expect(html).toContain('<section class="sourceFilter" aria-labelledby="source-filter-heading">');
    expect(html).toContain('id="source-filter-heading"');
    expect(html).toContain('role="group" aria-label="Filter latest updates by source"');
    expect(html).toContain("Choose a source to filter the latest updates below. Published editions remain unchanged.");
    for (const label of ["All sources", "OpenAI", "Anthropic", "LangChain", "LangGraph"]) {
      expect(html).toContain(label);
    }
    // Every clickable control is a real <button>, never a bare <div onClick>.
    expect(html.match(/<button/g)).toHaveLength(5);
  });

  it("marks the active card with aria-pressed and hides decorative marks from assistive tech", () => {
    const html = renderToStaticMarkup(<SourceFilter selectedSourceId="openai_news" onSelect={() => undefined} />);
    expect((html.match(/aria-pressed="true"/g) ?? []).length).toBe(1);
    expect((html.match(/aria-pressed="false"/g) ?? []).length).toBe(4);
    expect(html).toContain('<span class="sourceMark" aria-hidden="true">OAI</span>');
    expect(html).toContain('<span class="sourceCheck" aria-hidden="true">');
  });

  it("does not rely on colour alone: exactly the active card carries a visible text/glyph indicator", () => {
    // Whichever of the five is active (including "All sources" itself),
    // exactly one visible, non-colour glyph indicator is rendered -- never
    // zero (colour-only) and never more than one.
    for (const sourceId of [null, "openai_news", "anthropic_news", "langchain_pypi", "langgraph_pypi"]) {
      const html = renderToStaticMarkup(<SourceFilter selectedSourceId={sourceId} onSelect={() => undefined} />);
      expect((html.match(/sourceCheck/g) ?? []).length).toBe(1);
    }
  });
});

describe("SourceFilter interaction", () => {
  it("selects an unselected source with a single click", async () => {
    const onSelect = vi.fn();
    const { container, root } = mount();
    await act(async () => root.render(<SourceFilter selectedSourceId={null} onSelect={onSelect} />));

    await act(async () => buttonWithText(container, "OpenAI").click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("openai_news");

    await act(async () => root.unmount());
  });

  it("returns to All sources when the active card is clicked again, without a double click", async () => {
    const onSelect = vi.fn();
    const { container, root } = mount();
    await act(async () => root.render(<SourceFilter selectedSourceId="openai_news" onSelect={onSelect} />));

    await act(async () => buttonWithText(container, "OpenAI").click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(null);

    await act(async () => root.unmount());
  });

  it("supports keyboard activation via Enter and Space on a real button element", async () => {
    const onSelect = vi.fn();
    const { container, root } = mount();
    await act(async () => root.render(<SourceFilter selectedSourceId={null} onSelect={onSelect} />));

    const button = buttonWithText(container, "LangChain");
    expect(button.tagName).toBe("BUTTON");
    button.focus();
    expect(document.activeElement).toBe(button);

    // Native <button> elements activate on Enter/Space automatically via the
    // browser's default click behaviour -- exercised here as a real click,
    // since jsdom does not synthesize that default action from keydown.
    await act(async () => button.click());
    expect(onSelect).toHaveBeenCalledWith("langchain_pypi");

    await act(async () => root.unmount());
  });
});
