// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SubscribeForm, SubscriptionActionPage } from "./Subscriptions";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
  window.history.replaceState(null, "", "/");
});

describe("subscription UI", () => {
  it("submits explicit consent and renders the privacy-safe response", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
      message: "If the address is eligible, a confirmation email will be sent.",
    }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<SubscribeForm />));

    const input = container.querySelector("input");
    if (!input) throw new Error("email input missing");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, "reader@example.com");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector("form")?.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    ));

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      email: "reader@example.com", consent_to_daily_digest: true,
    });
    expect(container.textContent).toContain("If the address is eligible");
    await act(async () => root.unmount());
  });

  it("removes the fragment before requiring explicit confirmation", async () => {
    window.history.replaceState(null, "", "/subscriptions/confirm#token=secret-token");
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
      message: "Your subscription has been confirmed.",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<SubscriptionActionPage action="confirm" />));

    expect(window.location.hash).toBe("");
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => container.querySelector("button")?.click());
    expect(String(fetchMock.mock.calls[0]?.[1]?.body)).toContain("secret-token");
    expect(container.textContent).toContain("confirmed");
    await act(async () => root.unmount());
  });

  it("shows an invalid-link state without making a request", async () => {
    window.history.replaceState(null, "", "/subscriptions/unsubscribe");
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);

    await act(async () => root.render(<SubscriptionActionPage action="unsubscribe" />));

    expect(container.querySelector('[role="alert"]')?.textContent).toContain("invalid");
    expect(container.querySelector("button")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });

  it("shows loading and a safe error when unsubscribe fails", async () => {
    window.history.replaceState(null, "", "/subscriptions/unsubscribe#token=secret-token");
    let rejectRequest: ((reason: Error) => void) | undefined;
    const pending = new Promise<Response>((_resolve, reject) => { rejectRequest = reject; });
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(() => pending));
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<SubscriptionActionPage action="unsubscribe" />));

    await act(async () => container.querySelector("button")?.click());
    expect(container.querySelector('[role="status"]')?.textContent).toContain("Working");
    await act(async () => rejectRequest?.(new Error("network failure")));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("no longer usable");
    expect(container.textContent).not.toContain("secret-token");
    await act(async () => root.unmount());
  });
});
