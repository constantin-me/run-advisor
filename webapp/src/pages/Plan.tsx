import { useEffect, useState } from "react";
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
      <h1>Training Plan</h1>

      {!plan && <div className="card empty-state">No active plan yet. Ask your coach in Chat to build one.</div>}

      {plan && (
        <>
          <h2>{plan.title}</h2>

          <div className="row">
            <button className="btn btn-primary" onClick={syncToGarmin} disabled={busy}>
              Sync to Garmin
            </button>
          </div>
          {syncStatus && <p className="status-info">{syncStatus}</p>}

          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Description</th>
                  <th>Distance</th>
                  <th>Watch</th>
                  <th>Done</th>
                </tr>
              </thead>
              <tbody>
                {plan.workouts.map((w) => (
                  <tr key={w.id} style={w.done ? { opacity: 0.55, textDecoration: "line-through" } : undefined}>
                    <td>{w.date}</td>
                    <td>
                      <span className="badge badge-neutral">{w.type}</span>
                    </td>
                    <td>{w.description ?? "-"}</td>
                    <td>{w.distance_m ? `${(w.distance_m / 1000).toFixed(1)} km` : "-"}</td>
                    <td>{w.synced_to_garmin ? <span className="badge badge-success">synced</span> : "-"}</td>
                    <td>
                      <input
                        type="checkbox"
                        checked={w.done}
                        onChange={() => toggleDone(w)}
                        style={{ width: 18, height: 18 }}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
