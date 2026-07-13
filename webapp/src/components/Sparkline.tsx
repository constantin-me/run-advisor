import { useRef, useState } from "react";

type SparklineProps = {
  values: number[];
  dates?: string[];
  unit?: string;
  className?: string;
};

const WIDTH = 100;
const HEIGHT = 26;
const PAD = 3;

export default function Sparkline({ values, dates, unit, className }: SparklineProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  if (values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const coords = values.map((v, i) => {
    const x = (i / (values.length - 1)) * (WIDTH - PAD * 2) + PAD;
    const y = HEIGHT - PAD - ((v - min) / span) * (HEIGHT - PAD * 2);
    return { x, y };
  });

  function updateFromClientX(clientX: number) {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0) return;
    const fraction = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    setActiveIndex(Math.round(fraction * (values.length - 1)));
  }

  const active = activeIndex != null ? coords[activeIndex] : null;
  const tooltipLeftPct = active ? Math.min(88, Math.max(12, (active.x / WIDTH) * 100)) : 0;

  return (
    <div className="sparkline-wrap">
      <svg
        ref={svgRef}
        className={className}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        onMouseMove={(e) => updateFromClientX(e.clientX)}
        onMouseLeave={() => setActiveIndex(null)}
        onClick={(e) => updateFromClientX(e.clientX)}
      >
        <polyline
          points={coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ")}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {active && (
          <>
            <line x1={active.x} y1={0} x2={active.x} y2={HEIGHT} stroke="currentColor" strokeWidth={0.6} opacity={0.28} />
            <circle cx={active.x} cy={active.y} r={2.6} fill="currentColor" stroke="var(--color-bg-elevated)" strokeWidth={1.2} />
          </>
        )}
      </svg>
      {active && activeIndex != null && (
        <div className="sparkline-tooltip" style={{ left: `${tooltipLeftPct}%` }}>
          <strong>
            {values[activeIndex]}
            {unit ? ` ${unit}` : ""}
          </strong>
          {dates?.[activeIndex] && <span className="sparkline-tooltip-date"> · {dates[activeIndex]}</span>}
        </div>
      )}
    </div>
  );
}
