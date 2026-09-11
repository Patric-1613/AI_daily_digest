export class SubscriptionApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function post(path: string, body: object, apiBaseUrl: string): Promise<string> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "omit",
  });
  const payload = await response.json() as { message?: string; error?: { message?: string } };
  if (!response.ok) {
    throw new SubscriptionApiError(
      payload.error?.message ?? "The subscription request could not be completed.", response.status,
    );
  }
  return payload.message ?? "Request completed.";
}

export function requestSubscription(apiBaseUrl: string, email: string): Promise<string> {
  return post("/v1/subscriptions", { email, consent_to_daily_digest: true }, apiBaseUrl);
}

export function submitSubscriptionToken(
  apiBaseUrl: string, action: "confirm" | "unsubscribe", token: string,
): Promise<string> {
  return post(`/v1/subscriptions/${action}`, { token }, apiBaseUrl);
}
