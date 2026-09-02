import { Tooltip } from "antd";
import type { ReactNode } from "react";
import type { ReviewFlag, RoleVerdict } from "../../utils/role";
import { LOW_CONFIDENCE, ROLE_SCORE_THRESHOLD, SCORE_BAND_STRONG } from "../../utils/role";

/**
 * 机器判定的展示件：判定头条、复核旗标 chip、得分刻度。
 *
 * 存在的理由：队列里每一条都「待复核」，所以「待复核」本身没有信息量。有信息量的是
 * **机器自己报告的不确定性**——两个判定源打架、证据缺失、分类器推翻了自己。这些事实
 * 原本只存在于 role_reason 那一大段散文的中后段，复核者要逐条点开读到底才发现该看谁。
 * 这里把它们做成能扫读的部件，组头、列表、判据栏共用同一套口径与颜色。
 */

/**
 * 机器印记的字形：一枚带引脚的芯片。
 *
 * 用图形而不是再写四个字，是因为这个记号要出现在判定卡、表头、组头共四处——
 * 反复出现的是**同一个图形**，复核者第二次看到就知道「带芯片的都是机器说的」，
 * 而四段长短不一的中文标签学不出这种一致性。
 */
function MachineGlyph() {
  return (
    <svg
      className="review-machine-glyph"
      viewBox="0 0 16 16"
      width="13"
      height="13"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.25"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="3.5" y="3.5" width="9" height="9" rx="2" />
      <path d="M6.3 1.1v2.4M9.7 1.1v2.4M6.3 12.5v2.4M9.7 12.5v2.4M1.1 6.3h2.4M1.1 9.7h2.4M12.5 6.3h2.4M12.5 9.7h2.4" />
      <circle cx="8" cy="8" r="1.35" fill="currentColor" stroke="none" />
    </svg>
  );
}

/**
 * 「这是机器说的」的统一印记。
 *
 * 存在的理由：判定结果本身用的是语义色（业务对象=绿、关系表=紫……），单看那枚标签
 * 跟人工确认后的结果长得一模一样——复核者很容易把「机器猜的」当成「已经定了的」。
 * 所以把来源做成一个独立的、跨面板复用的记号：冷灰蓝 + 芯片字形 + 等宽小字，
 * 它是判定的**外框**，不参与语义配色，因此既不抢戏也不会被误读成结论。
 *
 * `bare` 给表头/组头这类本来就密的位置用：去掉底与框，只留字形与颜色。
 */
export function MachineMark({
  label = "机器判定",
  hint,
  bare = false,
  title,
}: {
  label?: string;
  /** 判定的状态，跟在一条细分隔线之后，例如「待你确认」。 */
  hint?: string;
  bare?: boolean;
  title?: string;
}) {
  const mark = (
    <span className={`review-machine${bare ? " review-machine--bare" : ""}`}>
      <MachineGlyph />
      <span className="review-machine-label">{label}</span>
      {hint && <span className="review-machine-hint">{hint}</span>}
    </span>
  );
  return title ? <Tooltip title={title}>{mark}</Tooltip> : mark;
}

/** 旗标 chip。`muted` 用于「全组共有」的旗标：说一次即可，不必每行都喊。 */
export function FlagChip({ flag, muted = false }: { flag: ReviewFlag; muted?: boolean }) {
  const chip = (
    <span className={`review-flag review-flag--${muted ? "muted" : flag.tone}`}>{flag.label}</span>
  );
  return flag.detail ? (
    <Tooltip title={flag.detail} placement="top">
      {chip}
    </Tooltip>
  ) : (
    chip
  );
}

/**
 * 一行的旗标组。超出 `max` 的折成 `+N`（仍可悬停看全）。
 * `commonKeys` 里的旗标压暗——组头已经讲过，行内留白给真正的差异。
 */
export function FlagChips({
  flags,
  hiddenKeys,
  max = 3,
}: {
  flags: ReviewFlag[];
  /** 全组共有的旗标：整条略去（组头已按 N/M 说过），这一列只留差异。 */
  hiddenKeys?: Set<string>;
  max?: number;
}) {
  if (flags.length === 0) return <span className="review-flag review-flag--clean">无异议</span>;
  const own = hiddenKeys ? flags.filter((f) => !hiddenKeys.has(f.key)) : flags;
  // 只剩全组共性 → 这一行没有自己的问题，破折号表示「与组头所述一致」。
  if (own.length === 0) return <span className="review-num">—</span>;
  const shown = own.slice(0, max);
  const rest = own.slice(max);
  return (
    <span className="review-flags">
      {shown.map((flag) => (
        <FlagChip key={flag.key} flag={flag} />
      ))}
      {rest.length > 0 && (
        <Tooltip title={rest.map((f) => f.detail || f.label).join("；")}>
          <span className="review-flag review-flag--muted">+{rest.length}</span>
        </Tooltip>
      )}
    </span>
  );
}

/**
 * 得分刻度：把「3.0」这个孤零零的数字放回它该在的位置——离阈值多远。
 * 阈值刻线是 2.0（object_classifier 判业务对象的门槛），3.0 起算证据充分。
 */
export function ScoreMeter({ score, band }: { score?: number; band: string }) {
  if (typeof score !== "number") return null;
  const max = Math.max(SCORE_BAND_STRONG + 2, score);
  const pct = (value: number) => `${Math.min(100, Math.max(0, (value / max) * 100))}%`;
  return (
    <span
      className="review-meter"
      aria-label={`综合得分 ${score.toFixed(1)}，阈值 ${ROLE_SCORE_THRESHOLD}`}
    >
      <span
        className={`review-meter-fill review-meter-fill--${band}`}
        style={{ width: pct(score) }}
      />
      <i className="review-meter-tick" style={{ left: pct(ROLE_SCORE_THRESHOLD) }} />
    </span>
  );
}

/** 机器判定卡的外壳：印记 + 一条待确认的横幅，内容由调用方填。 */
export function MachineVerdict({
  size = "md",
  hint = "待你确认",
  markTitle = "以下结论由分类器与 LLM 自动得出，尚未经人工确认——你的判定才是最终结果。",
  label,
  ariaLabel,
  children,
}: {
  size?: "md" | "lg";
  hint?: string;
  markTitle?: string;
  label?: string;
  ariaLabel?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`review-verdict review-verdict--${size}`}
      role="group"
      aria-label={ariaLabel ?? "机器判定，待人工确认"}
    >
      <MachineMark label={label} hint={hint} title={markTitle} />
      <span className="review-verdict-body">{children}</span>
    </div>
  );
}

/**
 * 判定头条：机器判成了什么、有多大把握。
 *
 * 角色是这块屏幕上最该被先读到的一句话，所以它比周边任何标签都重——但也正因为重，
 * 它必须同时说清「这话是谁说的」：一枚孤零零的绿色「业务对象」和人工敲定后的结果
 * 长得完全一样。所以整条被装进机器判定卡里：左侧斜纹导轨 + 芯片印记标明来源与
 * 「待你确认」的状态，卡内才是判定本身。
 */
export function VerdictHeadline({
  verdict,
  size = "md",
  note,
  reviewed = false,
}: {
  verdict: RoleVerdict;
  size?: "md" | "lg";
  /** 组头用：把握/得分取的是组内最低值，得说清楚，否则一条差的会读成全组都差。 */
  note?: string;
  /** 回看已判时：这条已经过人工确认，横幅不能再写「待你确认」——那是句假话。 */
  reviewed?: boolean;
}) {
  const confidence = verdict.confidence;
  // 低把握不能只是一个小号灰数字：机器自己没底恰恰是复核最该看的一条。
  const shaky = typeof confidence === "number" && confidence < LOW_CONFIDENCE;
  return (
    <MachineVerdict
      size={size}
      hint={reviewed ? "已确认" : undefined}
      markTitle={
        reviewed
          ? "这条结论由分类器与 LLM 得出、已经过人工确认。仍可改判，或退回复核重判。"
          : undefined
      }
      ariaLabel={`机器判定：${verdict.meta.label}${
        typeof confidence === "number" ? `，把握 ${Math.round(confidence * 100)}%` : ""
      }，${reviewed ? "已人工确认" : "待人工确认"}`}
    >
      <span className={`review-verdict-role review-verdict-role--${verdict.meta.cls}`}>
        {verdict.meta.label}
      </span>
      {typeof confidence === "number" && (
        <Tooltip title={note ?? (shaky ? "机器对这条判定把握不足，优先人工核实" : undefined)}>
          <span className={`review-verdict-conf${shaky ? " review-verdict-conf--shaky" : ""}`}>
            {note ? "最低把握" : "把握"} <b>{Math.round(confidence * 100)}%</b>
          </span>
        </Tooltip>
      )}
      {typeof verdict.score === "number" && (
        <Tooltip
          title={`综合得分 ${verdict.score.toFixed(1)}，≥ ${ROLE_SCORE_THRESHOLD.toFixed(
            1,
          )} 判为业务对象，≥ ${SCORE_BAND_STRONG.toFixed(1)} 算证据充分${note ? `（${note}）` : ""}`}
        >
          <span className="review-verdict-score">
            <ScoreMeter score={verdict.score} band={verdict.band} />
            <b>{verdict.score.toFixed(1)}</b>
            <em>{verdict.bandLabel}</em>
          </span>
        </Tooltip>
      )}
    </MachineVerdict>
  );
}

/**
 * 「为什么要你看」：把机器报告的不确定性摊开成一条条带说明的行，
 * 放在判据栏最上面——它比任何一个信号数值都更能决定这一条要不要多花时间。
 */
export function WhyReview({
  flags,
  title = "为什么要你看",
}: {
  flags: ReviewFlag[];
  title?: string;
}) {
  const notable = flags.filter((f) => f.tone !== "info");
  if (notable.length === 0) {
    return (
      <div className="review-why review-why--clean">
        <span className="review-flag review-flag--clean">无异议</span>
        机器没有报告分歧或证据缺口，按判据确认即可。
      </div>
    );
  }
  return (
    <div className="review-why">
      <div className="review-why-title">
        <MachineMark bare label={title} />
      </div>
      {notable.map((flag) => (
        <div key={flag.key} className="review-why-item">
          <span className={`review-flag review-flag--${flag.tone}`}>{flag.label}</span>
          {flag.detail && <span className="review-why-detail">{flag.detail}</span>}
        </div>
      ))}
    </div>
  );
}
