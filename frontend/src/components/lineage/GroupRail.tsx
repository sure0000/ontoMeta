import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { Button, Tooltip } from "antd";
import { LineageTableName } from "./LineageTableName";

/**
 * 逐组审核的队列。分组口径见 :func:`reviewGroups`——**同一个键连起来的那一坨**
 * （没有键的图则退化成普通连通分量）。
 *
 * 表一多，整张图就是一团毛线：实测 jwsp 138 张表 200 条线，整张图是一个 93 表的
 * 连通分量，一屏根本审不动。分完组是 9 组，每组一个语义、一个要回答的问题
 * ——「这 73 张表真的共用行政区划代码吗」。
 *
 * 三条取舍：
 *
 * 1. **放在左栏而不是画布浮层**：它是个队列（还剩几组没审），要能一眼看到全局进度；
 *    而且左栏本来就在，不占画布一寸空间。
 * 2. **选中一组＝画布只剩这一组的表和这一组的线**（过滤，不是压暗）：分组审核的意义
 *    就是这一屏只有这一组，压暗留不出空间来。切组时画布自动框住它。
 * 3. **孤立表合成一组**：一条线都没有的表各自都是一个分量，铺开会变成几十个
 *    「1 张表 0 条线」，把真正要审的组淹掉。
 */

import type { ReviewGroup } from "./graphLayout";

interface Props {
  components: ReviewGroup[];
  loners: string[];
  /** 正在审的组 id；null = 全部显示。 */
  active: string | null;
  onPick: (id: string | null) => void;
  onStep: (delta: number) => void;
  /** 当前组在 components 里的序号（-1 = 不在其中，比如正看孤立表那组）。 */
  index: number;
}

export const LONERS_GROUP = "__loners__";

export function GroupRail({ components, loners, active, onPick, onStep, index }: Props) {
  const total = components.length + (loners.length > 0 ? 1 : 0);
  const position =
    active === LONERS_GROUP ? total : index >= 0 ? index + 1 : 0;

  return (
    <>
      <div className="lin-group-bar">
        <Tooltip title="上一组">
          <Button
            size="small"
            type="text"
            icon={<LeftOutlined />}
            disabled={total === 0 || position <= 1}
            onClick={() => onStep(-1)}
          />
        </Tooltip>
        <span className="lin-group-pos">
          {active ? (
            <>
              第 <b>{position}</b> / {total} 组
            </>
          ) : (
            <>共 {total} 组</>
          )}
        </span>
        <Tooltip title="下一组">
          <Button
            size="small"
            type="text"
            icon={<RightOutlined />}
            disabled={total === 0 || position >= total}
            onClick={() => onStep(1)}
          />
        </Tooltip>
      </div>

      <ul className="lin-group-list">
        <li>
          <button
            type="button"
            className={`lin-group${active === null ? " lin-group--on" : ""}`}
            onClick={() => onPick(null)}
          >
            <span className="lin-group-name">全部显示</span>
            <span className="lin-group-meta">
              {new Set(components.flatMap((item) => item.tables)).size + loners.length} 表 ·{" "}
              {components.reduce((sum, item) => sum + item.edgeIds.length, 0)} 线
            </span>
          </button>
        </li>

        {components.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className={`lin-group${active === item.id ? " lin-group--on" : ""}`}
              onClick={() => onPick(item.id)}
            >
              <span className="lin-group-name">
                {item.label}
                {item.ordinal > 0 && <i className="lin-group-ord">#{item.ordinal}</i>}
                <em>
                  {item.tables.length} 表 · {item.edgeIds.length} 线
                </em>
              </span>
              {/* 组本身没有名字，只能靠成员认——列前三张表，认出"这是案件那一摊"就够了 */}
              <span className="lin-group-members">
                {item.tables.slice(0, 3).map((table) => (
                  <LineageTableName key={table} name={table} />
                ))}
                {item.tables.length > 3 && <i>…另有 {item.tables.length - 3} 张</i>}
              </span>
            </button>
          </li>
        ))}

        {loners.length > 0 && (
          <li>
            <button
              type="button"
              className={`lin-group lin-group--loners${
                active === LONERS_GROUP ? " lin-group--on" : ""
              }`}
              onClick={() => onPick(LONERS_GROUP)}
            >
              <span className="lin-group-name">
                无连线的表
                <em>{loners.length} 张</em>
              </span>
              <span className="lin-group-members">
                这些表一条线都没有，智能补录也没连上——要么手工连，要么从画布上移走
              </span>
            </button>
          </li>
        )}

        {components.length === 0 && loners.length === 0 && (
          <li className="lin-muted lin-rail-empty">画布是空的</li>
        )}
      </ul>
    </>
  );
}
