import type { ReactNode } from "react";

interface Props {
  title: ReactNode;
  count?: number;
  countPrimary?: boolean;
  icon?: ReactNode;
  extra?: ReactNode;
  bodyFlush?: boolean;
  children: ReactNode;
  style?: React.CSSProperties;
}

export function SectionCard({
  title,
  count,
  countPrimary,
  icon,
  extra,
  bodyFlush,
  children,
  style,
}: Props) {
  return (
    <section className="section-card" style={style}>
      <header className="section-card-head">
        <div className="section-card-head-title">
          {icon}
          <span>{title}</span>
          {typeof count === "number" && (
            <span
              className={
                countPrimary
                  ? "section-card-count section-card-count--primary"
                  : "section-card-count"
              }
            >
              {count}
            </span>
          )}
        </div>
        {extra && <div>{extra}</div>}
      </header>
      <div className={bodyFlush ? "section-card-body section-card-body--flush" : "section-card-body"}>
        {children}
      </div>
    </section>
  );
}
