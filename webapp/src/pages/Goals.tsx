import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";

type Goal = {
  id: number;
  text: string;
  target_date: string | null;
};

function formatTargetDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

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
      <div className="page-header">
        <div className="eyebrow">What you're chasing</div>
        <h1>Goals</h1>
      </div>

      {goals.length > 0 ? (
        <div className="item-list" style={{ marginBottom: 14 }}>
          {goals.map((g) => (
            <div className="goal-card" key={g.id}>
              <div className="goal-marker" />
              <div>
                <div className="goal-text">{g.text}</div>
                {g.target_date && <div className="goal-date">By {formatTargetDate(g.target_date)}</div>}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="card empty-state">No goals yet — add one below, or ask your coach to set one for you.</div>
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
            Add goal
          </button>
        </div>
      </div>
    </div>
  );
}
