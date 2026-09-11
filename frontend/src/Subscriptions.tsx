import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { publicConfig } from "./config";
import { requestSubscription, submitSubscriptionToken } from "./api/subscriptions";

export function SubscribeForm() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [message, setMessage] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState("loading");
    try {
      setMessage(await requestSubscription(publicConfig.apiBaseUrl, email));
      setState("success");
    } catch {
      setMessage("The subscription request could not be completed. Please try again.");
      setState("error");
    }
  }

  return (
    <form className="subscribeField" onSubmit={(event) => void submit(event)}>
      <label className="srOnly" htmlFor="email">Email address</label>
      <input id="email" type="email" maxLength={320} required value={email}
        onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" />
      <button disabled={state === "loading"} type="submit">
        {state === "loading" ? "Sending…" : "Subscribe"}
      </button>
      {state !== "idle" && state !== "loading" ? (
        <span role={state === "error" ? "alert" : "status"} className="subscriptionMessage">
          {message}
        </span>
      ) : null}
    </form>
  );
}

export function SubscriptionActionPage({ action }: { action: "confirm" | "unsubscribe" }) {
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("token"));
  const [state, setState] = useState<"ready" | "loading" | "success" | "error">(token ? "ready" : "error");
  const [message, setMessage] = useState(token ? "" : "This subscription link is invalid.");

  useEffect(() => { window.history.replaceState(null, "", window.location.pathname); }, []);

  async function submit() {
    if (!token) return;
    setState("loading");
    try {
      setMessage(await submitSubscriptionToken(publicConfig.apiBaseUrl, action, token));
      setState("success");
    } catch {
      setMessage("This subscription link is invalid or no longer usable.");
      setState("error");
    }
  }

  const verb = action === "confirm" ? "Confirm subscription" : "Unsubscribe";
  return (
    <main className="subscriptionPage">
      <section className="articleCard" aria-labelledby="subscription-action-heading">
        <p className="sectionLabel">AI Daily Digest</p>
        <h1 id="subscription-action-heading">{verb}</h1>
        {state === "ready" ? <button type="button" onClick={() => void submit()}>{verb}</button> : null}
        {state === "loading" ? <p role="status">Working…</p> : null}
        {state === "success" ? <p role="status">{message}</p> : null}
        {state === "error" ? <p role="alert">{message}</p> : null}
      </section>
    </main>
  );
}
