type SparklineProps = {
  values: number[];
  className?: string;
};

const WIDTH = 100;
const HEIGHT = 26;
const PAD = 3;

export default function Sparkline({ values, className }: SparklineProps) {
  if (values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const points = values.map((v, i) => {
    const x = (i / (values.length - 1)) * (WIDTH - PAD * 2) + PAD;
    const y = HEIGHT - PAD - ((v - min) / span) * (HEIGHT - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  return (
    <svg className={className} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none">
      <polyline points={points.join(" ")} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
