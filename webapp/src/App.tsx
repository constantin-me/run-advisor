import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import NavBar from "./components/NavBar";
import TelegramLoginGate from "./components/TelegramLoginGate";
import { bootstrapSession, type SessionResult } from "./lib/session";
import Chat from "./pages/Chat";
import Dashboard from "./pages/Dashboard";
import Goals from "./pages/Goals";
import Plan from "./pages/Plan";

export default function App() {
  const [status, setStatus] = useState<"loading" | SessionResult>("loading");

  useEffect(() => {
    const tg = (window as any).Telegram?.WebApp;
    tg?.ready();
    tg?.expand();

    bootstrapSession()
      .then(setStatus)
      .catch(() => setStatus("error"));
  }, []);

  if (status === "loading") return null;
  if (status === "needs-web-login") {
    return <TelegramLoginGate onSuccess={() => setStatus("ready")} />;
  }
  if (status === "error") {
    return <div style={{ padding: 16 }}>Could not start a session. Please reopen the app from Telegram.</div>;
  }

  return (
    <>
      <NavBar />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/goals" element={<Goals />} />
        <Route path="/plan" element={<Plan />} />
      </Routes>
    </>
  );
}
