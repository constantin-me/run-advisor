import { useEffect, useRef, useState } from "react";
import { apiFetch, streamChat } from "../lib/api";

type Message = {
  role: "user" | "assistant";
  content: string;
};

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  async function loadHistory() {
    const res = await apiFetch("/chat/history");
    if (res.ok) {
      const data = await res.json();
      setMessages(data.map((m: { role: string; content: string }) => ({ role: m.role, content: m.content })));
    }
    setLoaded(true);
  }

  useEffect(() => {
    loadHistory().catch(() => setLoaded(true));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;

    setInput("");
    setBusy(true);
    setMessages((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "" }]);

    const { done } = streamChat(text, (token) => {
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = { role: "assistant", content: next[next.length - 1].content + token };
        return next;
      });
    });

    await done;
    setBusy(false);
  }

  async function newConversation() {
    setBusy(true);
    try {
      await apiFetch("/chat/history", { method: "DELETE" });
      setMessages([]);
      setInput("");
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;

  return (
    <div className="page">
      <h1>Coach Chat</h1>

      {messages.length > 0 ? (
        <div className="chat-thread">
          {messages.map((m, i) => (
            <div key={i} className={`chat-bubble ${m.role}`}>
              {m.content || (m.role === "assistant" && busy && i === messages.length - 1 ? "…" : "")}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      ) : (
        <div className="card empty-state">No messages yet — say hello to your coach.</div>
      )}

      <div className="card stack">
        <textarea
          className="textarea"
          placeholder="Ask your coach anything…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={3}
        />
        <div className="row">
          <button className="btn btn-primary" onClick={send} disabled={busy || !input.trim()}>
            Send
          </button>
          <button className="btn" onClick={newConversation} disabled={busy}>
            New conversation
          </button>
        </div>
      </div>
    </div>
  );
}
