import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import App from "./App";
import { publicConfig } from "./config";

describe("AI Daily Digest shell", () => {
  it("uses the documented local API origin by default", () => {
    expect(publicConfig.apiBaseUrl).toBe("http://localhost:8000");
  });

  it("renders the core editorial sections", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).toContain("The signal in AI");
    expect(html).toContain("Source-backed AI industry monitoring");
    expect(html).toContain("0 updates loaded");
    expect(html).toContain("Illustrative model explorer");
    expect(html).toContain("Ask about today");
    expect(html).toContain("Illustrative trust metrics");
    expect(html).toContain("Loading source updates");
  });

  it.each(["Claude", "GPT-4", "Gemini", "DeepSeek", "Llama", "Grok"])(
    "includes the %s model family",
    (model) => {
      expect(renderToStaticMarkup(<App />)).toContain(model);
    },
  );
});
