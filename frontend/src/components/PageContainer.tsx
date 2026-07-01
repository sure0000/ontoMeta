import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
  full?: boolean;
}

export function PageContainer({ children, full }: Props) {
  return (
    <div className={`page-container fade-in${full ? " page-container--full" : ""}`}>
      {children}
    </div>
  );
}
