import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  EditOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";

export type StatusVisualTone = "primary" | "success" | "warning" | "neutral";

export function getOntologyDomainStatusVisual(status?: string | null): {
  tone: StatusVisualTone;
  icon: ReactNode;
} {
  switch (status) {
    case "draft":
      return { tone: "warning", icon: <EditOutlined /> };
    case "published":
      return { tone: "success", icon: <CheckCircleOutlined /> };
    case "in_review":
      return { tone: "warning", icon: <ClockCircleOutlined /> };
    case "archived":
      return { tone: "neutral", icon: <ClockCircleOutlined /> };
    case "active":
      return { tone: "primary", icon: <PlayCircleOutlined /> };
    default:
      return { tone: "primary", icon: <PlayCircleOutlined /> };
  }
}
