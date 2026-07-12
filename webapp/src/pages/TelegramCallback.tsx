import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { loginWithTelegramWidget } from "../lib/session";

export default function TelegramCallback({ onSuccess }: { onSuccess: () => void }) {
  const navigate = useNavigate();
  const [error, setError] = useState("");

  useEffect(() => {
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
