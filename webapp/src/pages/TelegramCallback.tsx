import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { loginWithTelegramWidget } from "../lib/session";

export default function TelegramCallback({ onSuccess }: { onSuccess: () => void }) {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const startedRef = useRef(false);

  useEffect(() => {
    // onSuccess (-> setStatus in the parent) triggers a re-render before the
    // navigate() below actually changes the route, which hands this effect
    // a new onSuccess reference and re-runs it while still mounted. Guard so
    // the login POST only ever fires once — the backend upsert is now also
    // race-safe on its own, but this avoids the wasted duplicate request.
    if (startedRef.current) return;
    startedRef.current = true;

    const params = Object.fromEntries(new URLSearchParams(window.location.search));
    if (!params.hash) {
      setError("Missing login data — please try logging in again.");
      return;
    }

    loginWithTelegramWidget(params).then((ok) => {
      if (ok) {
        onSuccess();
        navigate("/", { replace: true });
      } else {
        setError("Login failed — please try again.");
      }
    });
  }, [navigate, onSuccess]);

  return (
    <div className="page">
      <h1>Signing you in…</h1>
      {error && <p className="status-error">{error}</p>}
    </div>
  );
}
