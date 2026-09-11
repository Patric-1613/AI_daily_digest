import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { SubscriptionActionPage } from "./Subscriptions";
import "./styles.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Frontend root element was not found");
}

const path = window.location.pathname;
const action = path === "/subscriptions/confirm"
  ? "confirm"
  : path === "/subscriptions/unsubscribe" ? "unsubscribe" : null;

createRoot(root).render(
  <StrictMode>
    {action ? <SubscriptionActionPage action={action} /> : <App />}
  </StrictMode>,
);
