# Data Agent UI 设计系统

## 背景

2026-08-30 审计发现 `chat-bi.css` 2719 行中存在系统性版式散度：

| 维度 | 现状 | 目标 |
|---|---|---|
| 字号 | 16 种（9/10/11/12/12.5/13/13.5/14/15/16/16.5/19/22/25/28/30px） | 6 级阶梯 |
| 圆角 | 18 种（1/4/5/6/7/8/9/10/12/13/14/15/18/20/999px 等） | 4 级阶梯 |
| 颜色 | 216 处硬编码 hex + 216 处 `var(--om-*)` | 单一真源 |
| 间距 | 无系统，现场拍数字 | 8px 基准网格 |
| 渲染块 | 21 种块各自发明卡片外观 | 统一 `<BlockCard>` 原语 |

**三份设计令牌真源**并存且不同步：`tokens.css` / `main.tsx` ConfigProvider / `chat-bi.css` 硬编码。

**两套气泡样式**并存（626 行一套 + 2222 行覆盖层），后者写死 hex 绕开令牌系统。

**同屏两种代码块**：结构化 SQL 块（头部+复制+高亮）vs 正文围栏代码（裸 `<pre>`）。

## 设计原则

1. **单一真源**：所有设计值从 CSS 变量读取，TypeScript 通过 `getComputedStyle` 消费，不重复定义。
2. **语义色收口**：彩色卡片底（`#fff7f5` 桃色 / `#faf7ff` 紫色）统一为中性 `--om-surface`，语义色只用于图标/状态点/边框强调。
3. **块外观统一**：21 种渲染块共享同一套卡片容器规格（圆角/留白/标题栏/分隔线）。
4. **轻量依赖**：允许 `react-markdown` + `remark-gfm` + `prism-react-renderer`（总计 ~80KB gzipped），但保留手写 SQL 高亮器的轻量路径。

## 设计令牌阶梯

### 字号阶梯（6 级）

```css
:root {
  --om-text-xs: 11px;    /* 标签、辅助信息 */
  --om-text-sm: 12px;    /* 次要文本、表单说明 */
  --om-text-base: 14px;  /* 正文默认（与 antd 对齐） */
  --om-text-md: 15px;    /* 回答正文、卡片标题 */
  --om-text-lg: 16px;    /* 一级标题、重要操作 */
  --om-text-xl: 18px;    /* 欢迎标题、空状态 */
  /* 代码字号独立 */
  --om-code-sm: 12px;    /* 内联代码 */
  --om-code-base: 13px;  /* 代码块 */
}
```

### 圆角阶梯（4 级）

```css
:root {
  --om-radius-sm: 6px;   /* 标签、状态点 */
  --om-radius: 8px;      /* 按钮、输入框（与 antd 对齐） */
  --om-radius-md: 10px;  /* 卡片、代码块 */
  --om-radius-lg: 12px;  /* 大卡片、弹窗（与 antd 对齐） */
  --om-radius-full: 9999px; /* 圆形头像、pill */
}
```

### 间距阶梯（8px 基准网格）

```css
:root {
  --om-space-1: 4px;
  --om-space-2: 8px;
  --om-space-3: 12px;
  --om-space-4: 16px;
  --om-space-5: 20px;
  --om-space-6: 24px;
  --om-space-8: 32px;
  --om-space-10: 40px;
}
```

### 语义色（已有，保持不变）

```css
:root {
  --om-primary: #2563eb;
  --om-success: #16a34a;
  --om-warning: #d97706;
  --om-error: #dc2626;
  --om-neutral: #64748b;
  
  --om-text: #0f172a;
  --om-text-secondary: #475569;
  --om-text-tertiary: #7c8798;
  
  --om-surface: #ffffff;
  --om-surface-muted: #f8fafc;
  --om-bg: #f6f8fb;
  --om-border: #eef1f6;
  --om-border-strong: #e2e8f0;
}
```

## BlockCard 统一原语

所有 21 种渲染块共享同一套外观规格：

```tsx
interface BlockCardProps {
  /** 卡片语义类型，控制左边框色与图标色 */
  variant?: 'neutral' | 'primary' | 'success' | 'warning' | 'error';
  /** 卡片头部（可选） */
  title?: string;
  /** 头部右侧操作区（可选） */
  actions?: React.ReactNode;
  /** 卡片主体 */
  children: React.ReactNode;
  /** 额外 CSS 类 */
  className?: string;
}
```

### 规格

- **外边距**：`margin: var(--om-space-3) 0` （上下 12px，左右 0）
- **内边距**：`padding: var(--om-space-4) var(--om-space-4)` （16px 四周）
- **圆角**：`border-radius: var(--om-radius-md)` （10px）
- **边框**：`border: 1px solid var(--om-border)`，左边框根据 `variant` 加粗 3px 并着色
- **背景**：`background: var(--om-surface)` （统一白底，不再用彩色）
- **阴影**：`box-shadow: var(--om-shadow-sm)` （0 1px 2px rgba(15, 23, 42, 0.04)）

### 标题栏规格（当 `title` 存在时）

- **间距**：标题栏与主体间隔 `margin-bottom: var(--om-space-3)` （12px）
- **分隔线**：标题栏下方 1px 实线 `border-bottom: 1px solid var(--om-border)`
- **标题字号**：`font-size: var(--om-text-md)` （15px）
- **标题颜色**：`color: var(--om-text)` （#0f172a）
- **标题字重**：`font-weight: 600`

### 语义色映射

```css
.block-card--primary { border-left-color: var(--om-primary); }
.block-card--success { border-left-color: var(--om-success); }
.block-card--warning { border-left-color: var(--om-warning); }
.block-card--error { border-left-color: var(--om-error); }
.block-card--neutral { border-left-color: var(--om-neutral); }
```

## Markdown 排版规范

回答正文（`MarkdownLite` / `MarkdownBlock`）排版参数：

- **字号**：`font-size: var(--om-text-md)` （15px）
- **行高**：`line-height: 1.72` （26px，提升可读性）
- **段落间距**：`gap: var(--om-space-2)` （8px，块间距由 flexbox gap 控制）
- **列表缩进**：`margin-left: 20px`，嵌套列表每层额外 +20px
- **引用块**：左边框 3px `var(--om-primary)`，左内边距 12px，斜体
- **内联代码**：`font-size: var(--om-code-sm)`，`background: var(--om-surface-muted)`，`padding: 2px 6px`，`border-radius: var(--om-radius-sm)`

## 代码块统一规范

围栏代码（\`\`\`）与结构化 SQL 块共享同一套外观：

- **外边距**：`margin: var(--om-space-3) 0`
- **圆角**：`border-radius: var(--om-radius-md)`
- **背景**：`background: #0f172a` （深色主题）
- **边框**：`border: 1px solid #1e293b`
- **头部栏**：
  - 背景：`background: #1e293b`
  - 高度：`height: 36px`
  - 左侧：语言标签（大写、11px、等宽字体、`letter-spacing: 0.08em`）
  - 右侧：复制按钮（灰底、悬停变白）
- **正文**：
  - 字号：`font-size: var(--om-code-base)` （13px）
  - 行高：`line-height: 1.7`
  - 内边距：`padding: var(--om-space-4) 18px`
  - 字体：`font-family: var(--om-font-mono)`

### 语法高亮色板

```css
.code-keyword { color: #93c5fd; font-weight: 600; } /* 关键字：蓝 */
.code-string  { color: #fcd34d; }                   /* 字符串：黄 */
.code-number  { color: #f0abfc; }                   /* 数字：紫 */
.code-comment { color: #64748b; font-style: italic; } /* 注释：灰斜体 */
.code-function { color: #a78bfa; }                  /* 函数：浅紫 */
.code-operator { color: #94a3b8; }                  /* 操作符：浅灰 */
```

## 气泡样式（删除重复层）

**删除 626-703 行的第一套气泡样式**，保留 2222 行起的 "Data Agent conversation surface" 覆盖层，但：

1. 将所有硬编码 hex 替换为 CSS 变量
2. 用户气泡：
   - 背景：`var(--om-surface)`
   - 边框：`1px solid var(--om-border)`
   - 圆角：`var(--om-radius-lg)` （12px，比卡片略圆）
   - 内边距：`10px 15px`
3. 助手气泡：
   - 背景：透明（`background: transparent`）
   - 头像：`width: 28px; height: 28px; border-radius: var(--om-radius)`，纯色 `var(--om-primary)`（不用渐变）
   - 正文：无背景、无边框、无阴影，直接落在画布上

## 实施计划

### 阶段 A：建立设计令牌系统（~30 分钟）

1. **扩展 `tokens.css`**：新增字号/间距阶梯
2. **统一 `main.tsx` ConfigProvider**：从 CSS 变量读取（通过 `getComputedStyle`）
3. **清理 `chat-bi.css` 硬编码 hex**：全部替换为 `var(--om-*)`

### 阶段 B：抽取 BlockCard 原语（~45 分钟）

1. **新建 `BlockCard.tsx`**：统一卡片容器组件
2. **改造 21 个渲染块**：用 `<BlockCard>` 包裹，删除各自的卡片样式
3. **删除彩色卡片底**：`#fff7f5` / `#faf7ff` / `#fafbfc` 全部改为 `var(--om-surface)`

### 阶段 C：统一代码块（~30 分钟）

1. **抽取 `CodeBlock.tsx`**：头部栏 + 复制按钮 + 高亮
2. **引入 `prism-react-renderer`**：替换手写 SQL 分词器（保留作为 fallback）
3. **改造 `MarkdownLite`**：围栏代码走新 `<CodeBlock>`

### 阶段 D：删除气泡重复层（~15 分钟）

1. **删除 626-703 行**第一套气泡样式
2. **清理 2222 行起的覆盖层**：hex → CSS 变量，渐变头像 → 纯色

### 阶段 E：Markdown 排版打磨（~30 分钟）

1. **字号/行高/段距**对齐规范
2. **列表缩进**收敛到 20px 倍数
3. **表格样式**：数字右对齐、等宽数位、粘性表头

## 验证标准

完成后应满足：

- [ ] `chat-bi.css` 缩减至 ~2100 行（删除 ~600 行重复/散度代码）
- [ ] 零硬编码 hex（所有颜色从 `var(--om-*)` 读取）
- [ ] 字号收敛至 6 种、圆角收敛至 5 种
- [ ] 21 个渲染块外观一致（同圆角、同留白、同标题栏）
- [ ] 围栏代码与 SQL 块外观统一（都有头部+复制+高亮）
- [ ] antd ConfigProvider 主题值从 CSS 变量动态读取

## 参考

- antd 6 主题系统：https://ant.design/docs/react/customize-theme
- CSS 设计令牌最佳实践：Open Props、Radix Themes
- 代码高亮：prism-react-renderer (~20KB gzipped)
- Markdown 渲染：react-markdown + remark-gfm (~60KB gzipped)
