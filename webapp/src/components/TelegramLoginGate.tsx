import { useEffect, useRef, useState } from "react";
import { loginWithTelegramWidget } from "../lib/session";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

export default function TelegramLoginGate({ onSuccess }: { onSuccess: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    window.onTelegramAuth = async (user) => {
      const ok = await loginWithTelegramWidget(user);
      if (cancelled) return;
      if (ok) onSuccess();
      else setError("Login failed — please try again.");
    };

    (async () => {
      const res = await fetch("/api/auth/config");
      if (!res.ok || cancelled) {
        setError("Could not reach the server.");
        return;
      }
      const { bot_username } = await res.json();

      const script = document.createElement("script");
      script.src = "https://telegram.org/js/telegram-widget.js?22";
      script.async = true;
      script.setAttribute("data-telegram-login", bot_username);
      script.setAttribute("data-size", "large");
      script.setAttribute("data-onauth", "onTelegramAuth(user)");
      script.setAttribute("data-request-access", "write");
      containerRef.current?.appendChild(script);
    })();

    return () => {
      cancelled = true;
      delete window.onTelegramAuth;
    };
  }, [onSuccess]);

  return (
    <div className="page">
      <h1>Running Coach</h1>
      <div className="card stack">
        <p style={{ margin: 0 }}>Log in with Telegram to continue.</p>
        <div ref={containerRef} />
        {error && <p className="status-error">{error}</p>}
      </div>
    </div>
  );
}
