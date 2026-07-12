import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import NavBar from "./components/NavBar";
import { bootstrapSession } from "./lib/session";
import Chat from "./pages/Chat";
import Dashboard from "./pages/Dashboard";
import Goals from "./pages/Goals";
import Plan from "./pages/Plan";

export default function App() {
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    const tg = (window as any).Telegram?.WebApp;
    tg?.ready();
    tg?.expand();

    bootstrapSession()
      .then((ok) => setStatus(ok ? "ready" : "error"))
      .catch(() => setStatus("error"));
  }, []);

  if (status === "loading") return null;
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
