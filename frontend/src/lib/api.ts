export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8841";

const ACTOR_KEY = "tsl.actor";

export function getActor(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ACTOR_KEY) ?? "";
}

export function setActor(name: string): void {
  window.localStorage.setItem(ACTOR_KEY, name);
}

/** JSON request to the backend. Sends the saved actor name as X-Actor; throws with the API's `detail`. */
export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
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
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail ?? res.statusText));
  }
  return data as T;
}

export function inr(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)}%`;
}
