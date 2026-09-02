import type { ReactNode } from "react";

interface Props {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  iconTone?: "primary" | "success" | "warning";
  extra?: ReactNode;
  /** 整行事实条：与标题、动作区同处页头，但自己占满一行（页头是 flex-wrap 行，
   *  这一项 flex-basis:100% 必然换行）。不传则页头形状与从前完全一致。 */
  meta?: ReactNode;
  withBorder?: boolean;
}

export function PageHeader({
  title,
  description,
  icon,
  iconTone = "primary",
  extra,
  meta,
  withBorder = true,
}: Props) {
  return (
    <div className={`page-header${withBorder ? " page-header--with-border" : ""}`}>
      <div className="page-header-main">
        {icon && <div className={`page-header-icon page-header-icon--${iconTone}`}>{icon}</div>}
        <div className="page-header-text">
          <div className="page-header-title">{title}</div>
          {description && <div className="page-header-description">{description}</div>}
        </div>
      </div>
      {extra && <div className="page-header-extra">{extra}</div>}
      {meta && <div className="page-header-meta">{meta}</div>}
    </div>
  );
}
