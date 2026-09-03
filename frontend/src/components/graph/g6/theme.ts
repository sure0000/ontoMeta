import { LABELS, TONES, type Tone } from "../../StatusBadge";

/**
 * canvas 绘制不能读 CSS 自定义属性，这里镜像 tokens.css / layout.css 里对应的固定色值
 * （项目当前没有暗色模式，这些值不会运行时变化）。
 */
export const TONE_COLORS: Record<Tone, { bg: string; text: string; border: string; dot: string }> =
  {
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

/**
 * 选中态：整张图里**唯一**用实心填充的节点。
 *
 * 业务地图上的卡片全是白底，只换描边颜色在 30+ 张卡里根本认不出来——尤其是压暗层
 * 也还是白底，选中和没选中差的只是一圈 2px 蓝线。填充色是这块画布上唯一够强的信号，
 * 所以选中态直接翻成蓝底白字，扫一眼就知道自己点在哪。
 */
export const SELECTED_COLORS = {
  bg: "#1d4ed8",
  border: "#1e3a8a",
  ring: "rgba(29, 78, 216, 0.45)",
  title: "#ffffff",
  sub: "#dbeafe",
};

/**
 * 邻居态：跟选中的那个直接连着。
 *
 * 它和选中态必须**分得开**——此前两者共用 active，于是"我点的是哪个"和"它连着谁"
 * 长得一模一样，等于没选中。这里退成浅蓝底、深蓝字，比压暗层亮，但不跟蓝底白字抢。
 */
export const ACTIVE_COLORS = {
  bg: "#eff4ff",
  border: "#60a5fa",
  title: "#1e3a8a",
};

/**
 * 「刚看过的那一组」：鼠标移开 / 清掉搜索之后留下的痕迹。
 *
 * 没有这一层的话，手一挪开刚读出来的东西就全没了——想再确认一眼只能重新找到那个节点
 * 再悬浮一次。留个记号，视线就能离开画布去看别处，回头还认得住。
 *
 * **刻意不用蓝色系**：蓝色已经被「当前焦点」占满了（深蓝＝正指着的，浅蓝＝它的邻居），
 * 痕迹再用浅一号的蓝，就分不清「刚才看的」和「现在的邻居」。琥珀色像荧光笔划过的痕，
 * 语义正好，也不跟蓝色抢。
 *
 * **色阶压得很淡**是有意的：它是"待会儿回头找得到"的记号，不是当前焦点。用足饱和度的
 * 琥珀（amber-100/500）满屏铺开会比蓝色焦点还抢眼，本末倒置——所以退到 amber-50 的底 +
 * amber-300 的描边，认得出但不喊。痕迹**只标节点不标连线**：连线一亮就是一张黄网，
 * 盖过底下真正要读的结构。
 */
export const RECENT_COLORS = {
  bg: "#fffbeb",
  border: "#fcd34d",
  title: "#b45309",
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

// 板块视图里「板块外的邻居」：灰调虚线卡片，与实心的板块成员拉开层次，
// 让「这块业务的边界在哪」不用读文字就看得出来。
export const EXTERNAL_COLORS = {
  bg: "#f8fafc",
  border: "#cbd5e1",
  title: "#64748b",
  badgeBg: "#e2e8f0",
  badgeText: "#475569",
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
  {
    border: "#818cf8",
    borderHover: "#4f46e5",
    bg: "rgba(99, 102, 241, 0.16)",
    bgCollapsed: "rgba(99, 102, 241, 0.24)",
    name: "#4338ca",
    countBg: "rgba(99, 102, 241, 0.28)",
    countText: "#4338ca",
  },
  {
    border: "#38bdf8",
    borderHover: "#0284c7",
    bg: "rgba(14, 165, 233, 0.16)",
    bgCollapsed: "rgba(14, 165, 233, 0.24)",
    name: "#0369a1",
    countBg: "rgba(14, 165, 233, 0.28)",
    countText: "#0369a1",
  },
  {
    border: "#2dd4bf",
    borderHover: "#0d9488",
    bg: "rgba(20, 184, 166, 0.16)",
    bgCollapsed: "rgba(20, 184, 166, 0.24)",
    name: "#0f766e",
    countBg: "rgba(20, 184, 166, 0.28)",
    countText: "#0f766e",
  },
  {
    border: "#4ade80",
    borderHover: "#16a34a",
    bg: "rgba(34, 197, 94, 0.16)",
    bgCollapsed: "rgba(34, 197, 94, 0.24)",
    name: "#15803d",
    countBg: "rgba(34, 197, 94, 0.28)",
    countText: "#15803d",
  },
  {
    border: "#fbbf24",
    borderHover: "#d97706",
    bg: "rgba(245, 158, 11, 0.18)",
    bgCollapsed: "rgba(245, 158, 11, 0.26)",
    name: "#b45309",
    countBg: "rgba(245, 158, 11, 0.3)",
    countText: "#b45309",
  },
  {
    border: "#fb923c",
    borderHover: "#ea580c",
    bg: "rgba(249, 115, 22, 0.16)",
    bgCollapsed: "rgba(249, 115, 22, 0.24)",
    name: "#c2410c",
    countBg: "rgba(249, 115, 22, 0.28)",
    countText: "#c2410c",
  },
  {
    border: "#fb7185",
    borderHover: "#e11d48",
    bg: "rgba(244, 63, 94, 0.14)",
    bgCollapsed: "rgba(244, 63, 94, 0.22)",
    name: "#be123c",
    countBg: "rgba(244, 63, 94, 0.26)",
    countText: "#be123c",
  },
  {
    border: "#e879f9",
    borderHover: "#c026d3",
    bg: "rgba(217, 70, 239, 0.14)",
    bgCollapsed: "rgba(217, 70, 239, 0.22)",
    name: "#a21caf",
    countBg: "rgba(217, 70, 239, 0.26)",
    countText: "#a21caf",
  },
  {
    border: "#c084fc",
    borderHover: "#9333ea",
    bg: "rgba(168, 85, 247, 0.16)",
    bgCollapsed: "rgba(168, 85, 247, 0.24)",
    name: "#7e22ce",
    countBg: "rgba(168, 85, 247, 0.28)",
    countText: "#7e22ce",
  },
  {
    border: "#94a3b8",
    borderHover: "#475569",
    bg: "rgba(100, 116, 139, 0.14)",
    bgCollapsed: "rgba(100, 116, 139, 0.22)",
    name: "#334155",
    countBg: "rgba(100, 116, 139, 0.26)",
    countText: "#334155",
  },
];

export function comboColors(index: number): ComboColorSet {
  return COMBO_PALETTE[index % COMBO_PALETTE.length];
}

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 60;
// 紧凑卡片：模块关系图专用。一屏要同时容下 30+ 个对象还能读出名字，唯一的办法是
// 少花像素——只留名字，去掉状态徽标和副标题。卡片小了，同样的画布就能给更高的缩放。
export const NODE_COMPACT_WIDTH = 140;
export const NODE_COMPACT_HEIGHT = 38;
export const COMBO_HEADER_HEIGHT = 32;
