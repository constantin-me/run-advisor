import { useEffect, useState } from "react";
import Sparkline from "../components/Sparkline";
import { IconDownload, IconPulse, IconRefresh } from "../components/icons";
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

type MetricKey = keyof Omit<DailyMetric, "metric_date" | "sleep_duration_s">;

const STAT_DEFS: { key: MetricKey; label: string; unit?: string }[] = [
  { key: "hrv", label: "HRV", unit: "ms" },
  { key: "resting_hr", label: "Resting HR", unit: "bpm" },
  { key: "sleep_score", label: "Sleep" },
  { key: "body_battery", label: "Body Battery" },
  { key: "stress_avg", label: "Stress" },
  { key: "vo2max", label: "VO2 Max" },
  { key: "training_readiness", label: "Readiness" },
];

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

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
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

  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const weekActivities = activities.filter((a) => a.start_time && new Date(a.start_time).getTime() >= weekAgo);
  const weekKm = weekActivities.reduce((sum, a) => sum + (a.distance_m ?? 0), 0) / 1000;
  const lastActivity = activities[0];

  return (
    <div className="page">
      {linked === false && !mfaNeeded && (
        <>
          <div className="page-header">
            <div className="eyebrow">Get started</div>
            <h1>Link your Garmin</h1>
          </div>
          <div className="card stack">
            <p style={{ margin: 0 }}>Connect your Garmin account so your coach can see your training data.</p>
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
        </>
      )}

      {mfaNeeded && (
        <>
          <div className="page-header">
            <div className="eyebrow">One more step</div>
            <h1>Verify your account</h1>
          </div>
          <div className="card stack">
            <p style={{ margin: 0 }}>Enter the MFA code Garmin sent you.</p>
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
        </>
      )}

      {linked && (
        <>
          <div className="hero-card">
            <div className="hero-eyebrow">{greeting()}</div>
            <div className="hero-headline">{weekKm.toFixed(1)} km this week</div>
            <p className="hero-sub">
              {weekActivities.length > 0
                ? `${weekActivities.length} run${weekActivities.length === 1 ? "" : "s"} · last on ${formatDate(lastActivity?.start_time ?? null)}`
                : "No runs synced this week yet"}
            </p>
          </div>

          <div className="row">
            <button className="btn btn-ghost" onClick={handleSync} disabled={busy}>
              <span className="row" style={{ gap: 6 }}>
                <IconRefresh style={{ width: 15, height: 15 }} />
                Sync now
              </span>
            </button>
            <button className="btn btn-ghost" onClick={handleBackfill} disabled={busy}>
              <span className="row" style={{ gap: 6 }}>
                <IconDownload style={{ width: 15, height: 15 }} />
                Backfill history
              </span>
            </button>
          </div>
          {backfillStatus && <p className="status-info">{backfillStatus}</p>}

          <h2>Health data</h2>
          {metrics.length === 0 ? (
            <div className="card empty-state">No metrics synced yet.</div>
          ) : (
            <div className="stat-grid">
              {STAT_DEFS.filter((def) => metrics[0]?.[def.key] != null).map((def) => {
                const latest = metrics[0][def.key] as number;
                const sparkPairs = metrics
                  .slice(0, 14)
                  .map((m) => ({ value: m[def.key] as number | null, date: m.metric_date }))
                  .filter((p): p is { value: number; date: string } => p.value != null)
                  .reverse();
                return (
                  <div className="stat-tile" key={def.key}>
                    <div className="stat-label">{def.label}</div>
                    <div className="stat-value-row">
                      <span className="stat-value">{Math.round(latest)}</span>
                      {def.unit && <span className="stat-unit">{def.unit}</span>}
                    </div>
                    {sparkPairs.length >= 2 && (
                      <Sparkline
                        className="stat-sparkline"
                        values={sparkPairs.map((p) => p.value)}
                        dates={sparkPairs.map((p) => formatDate(p.date))}
                        unit={def.unit}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}

          <h2>Recent activities</h2>
          {activities.length === 0 ? (
            <div className="card empty-state">No activities synced yet.</div>
          ) : (
            <div className="item-list">
              {activities.slice(0, 15).map((a) => (
                <div className="item-row" key={a.garmin_activity_id}>
                  <div className="item-icon">
                    <IconPulse />
                  </div>
                  <div className="item-main">
                    <div className="item-title">{(a.activity_type ?? "activity").replace(/_/g, " ")}</div>
                    <div className="item-meta">
                      {formatDate(a.start_time)} · {formatPace(a.avg_pace_s_per_km)}
                    </div>
                  </div>
                  <div className="item-trail">
                    <div className="item-stat">{a.distance_m ? `${(a.distance_m / 1000).toFixed(2)} km` : "-"}</div>
                    {a.avg_hr && <div className="item-substat">{a.avg_hr} bpm</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
