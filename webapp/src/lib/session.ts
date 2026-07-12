let token: string | null = null;

export function setToken(t: string): void {
  token = t;
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

export async function bootstrapSession(): Promise<SessionResult> {
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
