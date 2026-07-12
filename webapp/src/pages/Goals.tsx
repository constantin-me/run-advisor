import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";

type Goal = {
  id: number;
  text: string;
  target_date: string | null;
};

export default function Goals() {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [text, setText] = useState("");
  const [targetDate, setTargetDate] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    const res = await apiFetch("/goals");
    if (res.ok) setGoals(await res.json());
  }

  useEffect(() => {
    load().catch(() => {});
  }, []);

  async function addGoal() {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await apiFetch("/goals", {
        method: "POST",
        body: JSON.stringify({ text, target_date: targetDate || null }),
      });
      setText("");
      setTargetDate("");
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <h1>Goals</h1>

      {goals.length > 0 ? (
        <ul className="plain">
          {goals.map((g) => (
            <li key={g.id}>
              {g.text}
              {g.target_date && <span className="status-info"> — by {g.target_date}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <div className="card empty-state">No goals yet.</div>
      )}

      <h2>Add a goal</h2>
      <div className="card stack">
        <div className="field">
          <label>Goal</label>
          <input className="input" placeholder="e.g. sub-50 10k" value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <div className="field">
          <label>Target date (optional)</label>
          <input className="input" type="date" value={targetDate} onChange={(e) => setTargetDate(e.target.value)} />
        </div>
        <div className="row">
          <button className="btn btn-primary" onClick={addGoal} disabled={busy || !text.trim()}>
            Add
          </button>
        </div>
      </div>
    </div>
  );
}
