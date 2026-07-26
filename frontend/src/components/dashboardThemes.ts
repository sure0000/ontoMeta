// 数据看板（Dashboard）主题预设与解析。
// 主题是 Dashboard 级属性（spec.theme），对看板内所有面板（Panel）生效：
// 通过 CSS 变量下发（accent → --om-primary，面板底色/文字/背景），
// 编辑器 / 只读页 / embed / public 共用同一套解析，切换即时且对外一致。

export interface DashboardThemeSpec {
  preset?: string;
  bg?: string;
  panelBg?: string;
  accent?: string;
  text?: string;
  palette?: string[];
}

export interface ResolvedDashboardTheme {
  preset: string;
  dark: boolean;
  bg: string;
  panelBg: string;
  accent: string;
  text: string;
  textSecondary: string;
  border: string;
  palette: string[];
}

interface ThemePreset extends Omit<ResolvedDashboardTheme, "preset"> {
  label: string;
}

const BLUE_PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#db2777", "#0891b2", "#7c3aed"];

export const DASHBOARD_THEME_PRESETS: Record<string, ThemePreset> = {
  light: {
    label: "浅色",
    dark: false,
    bg: "#f5f7fa",
    panelBg: "#ffffff",
    accent: "#2563eb",
    text: "#1f2937",
    textSecondary: "#64748b",
    border: "#e5e7eb",
    palette: BLUE_PALETTE,
  },
  dark: {
    label: "深色",
    dark: true,
    bg: "#0b1a2e",
    panelBg: "#12233d",
    accent: "#38bdf8",
    text: "#e2e8f0",
    textSecondary: "#94a3b8",
    border: "#1e3a5f",
    palette: ["#38bdf8", "#34d399", "#fbbf24", "#f472b6", "#22d3ee", "#a78bfa"],
  },
  tech: {
    label: "科技蓝（大屏）",
    dark: true,
    bg: "#060b18",
    panelBg: "#0e1b33",
    accent: "#22d3ee",
    text: "#cfe8ff",
    textSecondary: "#7ba7d0",
    border: "#153154",
    palette: ["#22d3ee", "#38bdf8", "#818cf8", "#2dd4bf", "#facc15", "#f472b6"],
  },
  emerald: {
    label: "清新绿",
    dark: false,
    bg: "#f0fdf4",
    panelBg: "#ffffff",
    accent: "#059669",
    text: "#064e3b",
    textSecondary: "#4b7a68",
    border: "#c7e9d5",
    palette: ["#059669", "#0891b2", "#65a30d", "#f59e0b", "#0d9488", "#7c3aed"],
  },
  violet: {
    label: "雅致紫",
    dark: false,
    bg: "#f6f5ff",
    panelBg: "#ffffff",
    accent: "#7c3aed",
    text: "#2e1065",
    textSecondary: "#6b5b95",
    border: "#e4defb",
    palette: ["#7c3aed", "#2563eb", "#db2777", "#f59e0b", "#0891b2", "#16a34a"],
  },
};

export const DASHBOARD_THEME_OPTIONS = Object.entries(DASHBOARD_THEME_PRESETS).map(
  ([value, p]) => ({ value, label: p.label }),
);

/** 合并主题预设与用户覆盖项，得到可直接渲染的解析结果。 */
export function resolveDashboardTheme(
  theme?: DashboardThemeSpec | null,
): ResolvedDashboardTheme {
  const presetKey = theme?.preset && DASHBOARD_THEME_PRESETS[theme.preset] ? theme.preset : "light";
  const base = DASHBOARD_THEME_PRESETS[presetKey];
  return {
    preset: presetKey,
    dark: base.dark,
    bg: theme?.bg || base.bg,
    panelBg: theme?.panelBg || base.panelBg,
    accent: theme?.accent || base.accent,
    text: theme?.text || base.text,
    textSecondary: base.textSecondary,
    border: base.border,
    palette: theme?.palette?.length ? theme.palette : base.palette,
  };
}

/** 把解析后的主题转成 CSS 变量对象（作为容器 inline style 下发给面板与图表）。 */
export function dashboardThemeVars(
  t: ResolvedDashboardTheme,
): Record<string, string> {
  return {
    background: t.bg,
    "--om-primary": t.accent,
    "--dashboard-accent": t.accent,
    "--dashboard-bg": t.bg,
    "--dashboard-panel-bg": t.panelBg,
    "--dashboard-text": t.text,
    "--dashboard-text-secondary": t.textSecondary,
    "--dashboard-border": t.border,
  } as Record<string, string>;
}
