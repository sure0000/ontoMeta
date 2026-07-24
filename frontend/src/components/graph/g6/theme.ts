import { LABELS, TONES, type Tone } from "../../StatusBadge";

/**
 * canvas 绘制不能读 CSS 自定义属性，这里镜像 tokens.css / layout.css 里对应的固定色值
 * （项目当前没有暗色模式，这些值不会运行时变化）。
 */
export const TONE_COLORS: Record<Tone, { bg: string; text: string; border: string; dot: string }> = {
  default: { bg: "#f1f5f9", text: "#475569", border: "#e2e8f0", dot: "#64748b" },
  blue: { bg: "#eff4ff", text: "#2563eb", border: "#bfdbfe", dot: "#2563eb" },
  cyan: { bg: "#ecfeff", text: "#0e7490", border: "#cffafe", dot: "#0e7490" },
  gold: { bg: "#fef3c7", text: "#b45309", border: "#fde68a", dot: "#d97706" },
  green: { bg: "#dcfce7", text: "#15803d", border: "#bbf7d0", dot: "#16a34a" },
  red: { bg: "#fee2e2", text: "#b91c1c", border: "#fecaca", dot: "#dc2626" },
  processing: { bg: "#eff4ff", text: "#2563eb", border: "#bfdbfe", dot: "#2563eb" },
};

export function statusLabel(status: string): string {
  return LABELS[status] || status;
}

export function statusColors(status: string) {
  const tone = TONES[status] || "default";
  return TONE_COLORS[tone];
}

export const NODE_COLORS = {
  bg: "#ffffff",
  border: "#e2e8f0",
  title: "#0f172a",
  hoverBorder: "#2563eb",
  centerBorder: "#2563eb",
  centerBgFrom: "#eff4ff",
  centerBgTo: "#ffffff",
  centerTitle: "#2563eb",
};

// 概览图的主干枢纽节点：深色实心、醒目，与浅色业务对象卡片形成"骨架 vs 血肉"的层次。
export const HUB_COLORS = {
  bg: "#1e293b",
  bgHover: "#0f172a",
  border: "#334155",
  title: "#f8fafc",
  badgeBg: "#f1f5f9",
  badgeText: "#334155",
};

export const HUB_WIDTH = 176;
export const HUB_HEIGHT = 60;

export const EDGE_COLORS = {
  stroke: "#94a3b8",
  hoverStroke: "#2563eb",
  label: "#475569",
  labelBg: "#ffffff",
};

export const GROUPED_EDGE_COLOR = "#818cf8";

export interface ComboColorSet {
  border: string;
  borderHover: string;
  bg: string;
  bgCollapsed: string;
  name: string;
  countBg: string;
  countText: string;
}

// 概览分块按聚类循环取色，避免所有分块同一个浅紫色糊在一起分不清边界。
export const COMBO_PALETTE: ComboColorSet[] = [
  { border: "#818cf8", borderHover: "#4f46e5", bg: "rgba(99, 102, 241, 0.16)", bgCollapsed: "rgba(99, 102, 241, 0.24)", name: "#4338ca", countBg: "rgba(99, 102, 241, 0.28)", countText: "#4338ca" },
  { border: "#38bdf8", borderHover: "#0284c7", bg: "rgba(14, 165, 233, 0.16)", bgCollapsed: "rgba(14, 165, 233, 0.24)", name: "#0369a1", countBg: "rgba(14, 165, 233, 0.28)", countText: "#0369a1" },
  { border: "#2dd4bf", borderHover: "#0d9488", bg: "rgba(20, 184, 166, 0.16)", bgCollapsed: "rgba(20, 184, 166, 0.24)", name: "#0f766e", countBg: "rgba(20, 184, 166, 0.28)", countText: "#0f766e" },
  { border: "#4ade80", borderHover: "#16a34a", bg: "rgba(34, 197, 94, 0.16)", bgCollapsed: "rgba(34, 197, 94, 0.24)", name: "#15803d", countBg: "rgba(34, 197, 94, 0.28)", countText: "#15803d" },
  { border: "#fbbf24", borderHover: "#d97706", bg: "rgba(245, 158, 11, 0.18)", bgCollapsed: "rgba(245, 158, 11, 0.26)", name: "#b45309", countBg: "rgba(245, 158, 11, 0.3)", countText: "#b45309" },
  { border: "#fb923c", borderHover: "#ea580c", bg: "rgba(249, 115, 22, 0.16)", bgCollapsed: "rgba(249, 115, 22, 0.24)", name: "#c2410c", countBg: "rgba(249, 115, 22, 0.28)", countText: "#c2410c" },
  { border: "#fb7185", borderHover: "#e11d48", bg: "rgba(244, 63, 94, 0.14)", bgCollapsed: "rgba(244, 63, 94, 0.22)", name: "#be123c", countBg: "rgba(244, 63, 94, 0.26)", countText: "#be123c" },
  { border: "#e879f9", borderHover: "#c026d3", bg: "rgba(217, 70, 239, 0.14)", bgCollapsed: "rgba(217, 70, 239, 0.22)", name: "#a21caf", countBg: "rgba(217, 70, 239, 0.26)", countText: "#a21caf" },
  { border: "#c084fc", borderHover: "#9333ea", bg: "rgba(168, 85, 247, 0.16)", bgCollapsed: "rgba(168, 85, 247, 0.24)", name: "#7e22ce", countBg: "rgba(168, 85, 247, 0.28)", countText: "#7e22ce" },
  { border: "#94a3b8", borderHover: "#475569", bg: "rgba(100, 116, 139, 0.14)", bgCollapsed: "rgba(100, 116, 139, 0.22)", name: "#334155", countBg: "rgba(100, 116, 139, 0.26)", countText: "#334155" },
];

export function comboColors(index: number): ComboColorSet {
  return COMBO_PALETTE[index % COMBO_PALETTE.length];
}

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 56;
export const COMBO_HEADER_HEIGHT = 32;
