export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8841";

const ACTOR_KEY = "tsl.actor";
const TOKEN_KEY = "tsl.token";

export function getActor(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ACTOR_KEY) ?? "";
}

export function setActor(name: string): void {
  window.localStorage.setItem(ACTOR_KEY, name);
}

/** A login session token (from /login) or a static API token (`uv run python -m app.auth new-token NAME`).
 * Its name becomes the audit actor. */
export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export class AuthError extends Error {}

/** JSON request to the backend. Sends the API token as a Bearer token (or, without one, the saved actor
 * name as X-Actor); throws with the API's `detail`, as an AuthError on 401/403. */
export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const actor = getActor();
  if (actor) headers["X-Actor"] = actor;
  const res = await fetch(`${API}${path}`, {
    method: init.method ?? (init.body !== undefined ? "POST" : "GET"),
    headers,
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail ?? res.statusText);
    if (res.status === 401 && path !== "/api/auth/login" && window.location.pathname !== "/login") {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    if (res.status === 401 || res.status === 403) throw new AuthError(`${message} — log in or set an API token`);
    throw new Error(message);
  }
  return data as T;
}

export type CredentialFieldStatus = {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  source: "db" | "env" | null;
  fingerprint: string | null;
  updated_at: string | null;
};

export type Broker = {
  name: string;
  label: string;
  description: string;
  modes: string[];
  has_quotes: boolean;
  fields: CredentialFieldStatus[];
  configured: boolean;
};

export type BrokerAccount = {
  id: string;
  broker: string;
  account_ref_masked: string;
  mode: string;
  default_algo_id: string | null;
  registered: boolean;
  supports_mode?: boolean;
  configured: boolean;
  fields: CredentialFieldStatus[];
};

export function inr(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)}%`;
}
