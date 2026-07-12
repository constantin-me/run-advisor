import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";

type DailyMetric = {
  metric_date: string;
  sleep_score: number | null;
  sleep_duration_s: number | null;
  hrv: number | null;
  resting_hr: number | null;
  stress_avg: number | null;
  body_battery: number | null;
  vo2max: number | null;
  training_readiness: number | null;
};

type Activity = {
  garmin_activity_id: string;
  activity_type: string | null;
  start_time: string | null;
  distance_m: number | null;
  duration_s: number | null;
  avg_pace_s_per_km: number | null;
  avg_hr: number | null;
};

function formatPace(secPerKm: number | null): string {
  if (!secPerKm) return "-";
  const min = Math.floor(secPerKm / 60);
  const sec = Math.round(secPerKm % 60);
  return `${min}:${sec.toString().padStart(2, "0")}/km`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function Dashboard() {
  const [linked, setLinked] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaNeeded, setMfaNeeded] = useState(false);
  const [mfaCode, setMfaCode] = useState("");
  const [linkError, setLinkError] = useState("");
  const [busy, setBusy] = useState(false);

  const [metrics, setMetrics] = useState<DailyMetric[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);
  const [backfillStatus, setBackfillStatus] = useState("");

  async function loadData() {
    const statusRes = await apiFetch("/garmin/status");
    const isLinked = statusRes.ok && (await statusRes.json()).status === "linked";
    setLinked(isLinked);
    if (!isLinked) return;

    const [mRes, aRes] = await Promise.all([apiFetch("/metrics?days=30"), apiFetch("/activities?limit=50")]);
    if (mRes.ok) setMetrics(await mRes.json());
    if (aRes.ok) setActivities(await aRes.json());
  }

  useEffect(() => {
    loadData().catch(() => {});
  }, []);

  async function handleLink() {
    setBusy(true);
    setLinkError("");
    try {
      const res = await apiFetch("/garmin/link", { method: "POST", body: JSON.stringify({ email, password }) });
      const data = await res.json();
      if (!res.ok) {
        setLinkError(data.detail ?? "Link failed");
        return;
      }
      if (data.status === "mfa_required") {
        setMfaNeeded(true);
      } else {
        setLinked(true);
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleMfa() {
    setBusy(true);
    setLinkError("");
    try {
      const res = await apiFetch("/garmin/mfa", { method: "POST", body: JSON.stringify({ code: mfaCode }) });
      const data = await res.json();
      if (!res.ok) {
        setLinkError(data.detail ?? "MFA failed");
        return;
      }
      setMfaNeeded(false);
      setLinked(true);
    } finally {
      setBusy(false);
    }
  }

  async function handleSync() {
    setBusy(true);
    try {
      await apiFetch("/garmin/sync", { method: "POST" });
      await loadData();
    } finally {
      setBusy(false);
    }
  }

  async function handleBackfill() {
    setBusy(true);
    setBackfillStatus("Backfilling history, this can take a minute…");
    try {
      const res = await apiFetch("/garmin/backfill", { method: "POST", body: JSON.stringify({ days: 30 }) });
      const data = await res.json();
      if (!res.ok) {
        setBackfillStatus(data.detail ?? "Backfill failed");
        return;
      }
      setBackfillStatus(
        `Done: ${data.activities_synced} activities, ${data.metrics_days_synced} days of metrics.`
      );
      await loadData();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <h1>Dashboard</h1>

      {linked === false && !mfaNeeded && (
        <div className="card stack">
          <p style={{ margin: 0 }}>Link your Garmin account to get started.</p>
          <div className="field">
            <label>Garmin email</label>
            <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="row">
            <button className="btn btn-primary" onClick={handleLink} disabled={busy}>
              Link Garmin
            </button>
          </div>
          {linkError && <p className="status-error">{linkError}</p>}
        </div>
      )}

      {mfaNeeded && (
        <div className="card stack">
          <p style={{ margin: 0 }}>Enter the MFA code sent to you by Garmin.</p>
          <div className="field">
            <label>Code</label>
            <input className="input" placeholder="123456" value={mfaCode} onChange={(e) => setMfaCode(e.target.value)} />
          </div>
          <div className="row">
            <button className="btn btn-primary" onClick={handleMfa} disabled={busy}>
              Confirm
            </button>
          </div>
          {linkError && <p className="status-error">{linkError}</p>}
        </div>
      )}

      {linked && (
        <>
          <div className="row">
            <button className="btn" onClick={handleSync} disabled={busy}>
              Sync now
            </button>
            <button className="btn" onClick={handleBackfill} disabled={busy}>
              Backfill history
            </button>
          </div>
          {backfillStatus && <p className="status-info">{backfillStatus}</p>}

          <h2>Recent metrics</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Sleep</th>
                  <th>HRV</th>
                  <th>RHR</th>
                  <th>Stress</th>
                  <th>Body Battery</th>
                  <th>VO2max</th>
                  <th>Readiness</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((m) => (
                  <tr key={m.metric_date}>
                    <td>{formatDate(m.metric_date)}</td>
                    <td>{m.sleep_score ?? "-"}</td>
                    <td>{m.hrv ?? "-"}</td>
                    <td>{m.resting_hr ?? "-"}</td>
                    <td>{m.stress_avg ?? "-"}</td>
                    <td>{m.body_battery ?? "-"}</td>
                    <td>{m.vo2max ?? "-"}</td>
                    <td>{m.training_readiness ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {metrics.length === 0 && <div className="empty-state">No metrics synced yet.</div>}
          </div>

          <h2>Recent activities</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Distance</th>
                  <th>Pace</th>
                  <th>Avg HR</th>
                </tr>
              </thead>
              <tbody>
                {activities.map((a) => (
                  <tr key={a.garmin_activity_id}>
                    <td>{formatDate(a.start_time)}</td>
                    <td>
                      <span className="badge badge-neutral">{a.activity_type ?? "-"}</span>
                    </td>
                    <td>{a.distance_m ? `${(a.distance_m / 1000).toFixed(2)} km` : "-"}</td>
                    <td>{formatPace(a.avg_pace_s_per_km)}</td>
                    <td>{a.avg_hr ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {activities.length === 0 && <div className="empty-state">No activities synced yet.</div>}
          </div>
        </>
      )}
    </div>
  );
}
