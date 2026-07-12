const STORAGE_KEY = "garmin_coach_token";

let token: string | null = localStorage.getItem(STORAGE_KEY);

export function setToken(t: string): void {
  token = t;
  localStorage.setItem(STORAGE_KEY, t);
}

export function clearToken(): void {
  token = null;
  localStorage.removeItem(STORAGE_KEY);
}

export function getToken(): string | null {
  return token;
}

export type SessionResult = "ready" | "needs-web-login" | "error";

function getInitData(): string {
  return (window as any).Telegram?.WebApp?.initData ?? "";
}

async function postSession(path: string, body?: object): Promise<boolean> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) return false;

  const { token: sessionToken } = await res.json();
  setToken(sessionToken);
  return true;
}

async function hasValidStoredToken(): Promise<boolean> {
  if (!token) return false;
  const res = await fetch("/api/auth/whoami", { headers: { Authorization: `Bearer ${token}` } });
  return res.ok;
}

export async function bootstrapSession(): Promise<SessionResult> {
  if (await hasValidStoredToken()) return "ready";
  clearToken();

  const initData = getInitData();
  if (initData) {
    return (await postSession("/api/auth/session", { init_data: initData })) ? "ready" : "error";
  }

  // Silent local-dev shortcut — only succeeds if the backend has DEV_MODE=true,
  // a harmless 404 otherwise.
  if (await postSession("/api/auth/dev-session")) return "ready";

  return "needs-web-login";
}

export async function loginWithTelegramWidget(data: Record<string, unknown>): Promise<boolean> {
  return postSession("/api/auth/telegram-login", data);
}
