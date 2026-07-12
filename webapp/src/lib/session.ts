let token: string | null = null;

export function setToken(t: string): void {
  token = t;
}

export function getToken(): string | null {
  return token;
}

export async function bootstrapSession(): Promise<boolean> {
  const initData = (window as any).Telegram?.WebApp?.initData ?? "";

  const res = await fetch(initData ? "/api/auth/session" : "/api/auth/dev-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: initData ? JSON.stringify({ init_data: initData }) : undefined,
  });
  if (!res.ok) return false;

  const { token: sessionToken } = await res.json();
  setToken(sessionToken);
  return true;
}
