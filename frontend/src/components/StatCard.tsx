import type { ReactNode } from "react";

type Tone = "primary" | "success" | "warning" | "neutral";

interface Props {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}

export function StatCard({ icon, label, value, hint, tone = "primary" }: Props) {
  return (
    <div className="stat-card">
      <div className={`stat-card-icon stat-card-icon--${tone}`}>{icon}</div>
      <div className="stat-card-meta">
        <span className="stat-card-label">{label}</span>
        <span className="stat-card-value">{value}</span>
        {hint && <span className="stat-card-hint">{hint}</span>}
      </div>
    </div>
  );
}
