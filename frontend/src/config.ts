const DEFAULT_API_BASE_URL = "http://localhost:8000";

export const publicConfig = Object.freeze({
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL,
  subscriptionsEnabled: import.meta.env.VITE_SUBSCRIPTIONS_ENABLED === "true",
});
