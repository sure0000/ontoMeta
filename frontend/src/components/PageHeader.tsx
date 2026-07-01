import type { ReactNode } from "react";

interface Props {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  iconTone?: "primary" | "success" | "warning";
  extra?: ReactNode;
  withBorder?: boolean;
}

export function PageHeader({
  title,
  description,
  icon,
  iconTone = "primary",
  extra,
  withBorder = true,
}: Props) {
  return (
    <div className={`page-header${withBorder ? " page-header--with-border" : ""}`}>
      <div className="page-header-main">
        {icon && (
          <div className={`page-header-icon page-header-icon--${iconTone}`}>
            {icon}
          </div>
        )}
        <div className="page-header-text">
          <div className="page-header-title">{title}</div>
          {description && <div className="page-header-description">{description}</div>}
        </div>
      </div>
      {extra && <div className="page-header-extra">{extra}</div>}
    </div>
  );
}
