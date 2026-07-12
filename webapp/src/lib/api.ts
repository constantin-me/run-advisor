import { getToken } from "./session";

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`/api${path}`, { ...init, headers });
}

export type StreamHandle = {
  /** Resolves once the stream ends (successfully, on error, or aborted). */
  done: Promise<void>;
  abort: () => void;
};

export function streamChat(text: string, onToken: (token: string) => void): StreamHandle {
  const controller = new AbortController();

  const done = (async () => {
    try {
      const res = await apiFetch("/chat/stream", {
        method: "POST",
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });
      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done: readerDone, value } = await reader.read();
        if (readerDone) break;
        onToken(decoder.decode(value, { stream: true }));
      }
    } catch {
      // aborted or network error — caller decides how to surface this
    }
  })();

  return { done, abort: () => controller.abort() };
}
