import { useEffect, useRef, useState } from "react";

export default function TelegramLoginGate() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    (async () => {
      const res = await fetch("/api/auth/config");
      if (!res.ok || cancelled) {
        setError("Could not reach the server.");
        return;
      }
      const { bot_username } = await res.json();
      if (cancelled) return;

      // Redirect mode (data-auth-url), not the popup + JS-callback mode —
      // popups get silently blocked in enough browsers that the callback
      // mode is unreliable. This does a normal full-page navigation back
      // to /telegram-callback with the signed auth data as query params.
      const script = document.createElement("script");
      script.src = "https://telegram.org/js/telegram-widget.js?22";
      script.async = true;
      script.setAttribute("data-telegram-login", bot_username);
      script.setAttribute("data-size", "large");
      script.setAttribute("data-auth-url", `${window.location.origin}/telegram-callback`);
      script.setAttribute("data-request-access", "write");
      containerRef.current?.appendChild(script);
    })();

    return () => {
      cancelled = true;
    };
  }, []);

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
