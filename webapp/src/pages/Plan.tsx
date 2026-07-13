import { useEffect, useState } from "react";
import { IconCheck, IconWatch } from "../components/icons";
import { apiFetch } from "../lib/api";

type Workout = {
  id: number;
  date: string;
  type: string;
  description: string | null;
  distance_m: number | null;
  pace_s_per_km: number | null;
  done: boolean;
  synced_to_garmin: boolean;
};

type Plan = {
  id: number;
  title: string;
  workouts: Workout[];
};

function formatWorkoutDate(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export default function Plan() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [syncStatus, setSyncStatus] = useState("");

  async function load() {
    const res = await apiFetch("/plan");
    if (res.ok) setPlan(await res.json());
    setLoaded(true);
  }

  useEffect(() => {
    load().catch(() => setLoaded(true));
  }, []);

  async function toggleDone(workout: Workout) {
    const next = !workout.done;
    setPlan((p) =>
      p ? { ...p, workouts: p.workouts.map((w) => (w.id === workout.id ? { ...w, done: next } : w)) } : p
    );
    await apiFetch(`/plan/workouts/${workout.id}`, { method: "PATCH", body: JSON.stringify({ done: next }) });
  }

  async function syncToGarmin() {
    setBusy(true);
    setSyncStatus("Pushing workouts to Garmin Connect…");
    try {
      const res = await apiFetch("/plan/sync-garmin", { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        setSyncStatus(data.detail ?? "Sync failed");
        return;
      }
      setSyncStatus(
        `Done: ${data.created} workout${data.created === 1 ? "" : "s"} sent to your watch` +
          (data.removed_stale ? `, ${data.removed_stale} outdated workout${data.removed_stale === 1 ? "" : "s"} removed` : "") +
          (data.skipped_rest ? `, ${data.skipped_rest} rest day${data.skipped_rest === 1 ? "" : "s"} skipped` : "") +
          (data.failed ? `, ${data.failed} failed` : "") +
          "."
      );
      await load();
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;

  return (
    <div className="page">
      <div className="page-header">
        <div className="eyebrow">Your training</div>
        <h1>Plan</h1>
      </div>

      {!plan && <div className="card empty-state">No active plan yet. Ask your coach in Chat to build one.</div>}

      {plan && (
        <>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }}>
            <h2 style={{ margin: 0 }}>{plan.title}</h2>
            <button className="btn btn-primary" onClick={syncToGarmin} disabled={busy}>
              Sync to Garmin
            </button>
          </div>
          {syncStatus && <p className="status-info" style={{ marginBottom: 14 }}>{syncStatus}</p>}

          <div className="item-list">
            {plan.workouts.map((w) => (
              <div className={`workout-card${w.done ? " done" : ""}`} key={w.id}>
                <button
                  className={`workout-checkbox${w.done ? " checked" : ""}`}
                  onClick={() => toggleDone(w)}
                  aria-label={w.done ? "Mark as not done" : "Mark as done"}
                >
                  {w.done && <IconCheck />}
                </button>
                <div className="workout-main">
                  <div className="workout-date">{formatWorkoutDate(w.date)}</div>
                  <div className="workout-type">{w.type}</div>
                  {w.description && <div className="workout-desc">{w.description}</div>}
                </div>
                <div className="workout-trail">
                  {w.distance_m && <div>{(w.distance_m / 1000).toFixed(1)} km</div>}
                  {w.synced_to_garmin && (
                    <div className="row" style={{ justifyContent: "flex-end", gap: 4, marginTop: 3 }}>
                      <IconWatch style={{ width: 13, height: 13 }} />
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
