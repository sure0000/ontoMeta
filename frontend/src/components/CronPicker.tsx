/** 定时策略选择器：任意标准 cron 表达式，全下拉编辑 + 中文回读。
 *
 * 编辑器用 `react-js-cron`（antd 原生实现，周期/月/日/星期/时/分 六组下拉，覆盖
 * 标准 5 段 cron 的全部组合），回读用 `cronstrue` 的 zh_CN 本地化——两者都是成熟
 * 库，自己拼预置项既盖不全场景，也没法把已有表达式翻回人话。
 *
 * 不手填表达式：cron 写错要等到调度那一刻才暴露，而下拉编辑器产出的值恒定合法。
 */

import { Button, Popover, Space, Typography } from "antd";
import { ClockCircleOutlined } from "@ant-design/icons";
import cronstrue from "cronstrue/i18n";
import { useEffect, useState } from "react";
import { Cron } from "react-js-cron";
import type { CronError, Locale } from "react-js-cron";
import "react-js-cron/dist/styles.css";

// react-js-cron 未内置中文，按其 Locale 结构给一份。
const ZH_LOCALE: Locale = {
  everyText: "每",
  emptyMonths: "每月",
  emptyMonthDays: "每天",
  emptyMonthDaysShort: "日",
  emptyWeekDays: "每星期",
  emptyWeekDaysShort: "星期",
  emptyHours: "每小时",
  emptyMinutes: "每分钟",
  emptyMinutesForHourPeriod: "每",
  yearOption: "年",
  monthOption: "月",
  weekOption: "周",
  dayOption: "天",
  hourOption: "小时",
  minuteOption: "分钟",
  rebootOption: "重启时",
  prefixPeriod: "每",
  prefixMonths: "的",
  prefixMonthDays: "的",
  prefixWeekDays: "的",
  prefixWeekDaysForMonthAndYearPeriod: "并且",
  prefixHours: "的",
  prefixMinutes: ":",
  prefixMinutesForHourPeriod: "的第",
  suffixMinutesForHourPeriod: "分钟",
  errorInvalidCron: "cron 表达式不合法",
  clearButtonText: "清空",
  weekDays: ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"],
  months: [
    "一月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "十一月",
    "十二月",
  ],
};

/** 新建定时策略时的起始表达式（每天 02:00），只在用户从「不定时」切到定时时用。 */
const DEFAULT_CRON = "0 2 * * *";

/** cron → 中文描述。空 = 不定时；解析不了则原样回显，不假装看懂。 */
function describeCron(expr?: string | null): string {
  const value = expr?.trim();
  if (!value) return "不定时";
  try {
    return cronstrue.toString(value, {
      locale: "zh_CN",
      use24HourTimeFormat: true,
      throwExceptionOnParseError: true,
    });
  } catch {
    return value;
  }
}

interface Props {
  /** 当前 cron 表达式；空串 = 不定时。 */
  value: string;
  onChange: (value: string) => void;
  size?: "small" | "middle";
  disabled?: boolean;
}

/** 行内触发器：显示中文描述，点开在浮层里编辑，确定后才回写。 */
export function CronPicker({ value, onChange, size = "small", disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value || DEFAULT_CRON);
  const [error, setError] = useState<string | null>(null);

  // 每次打开都以当前值起步：上次取消留下的草稿不该粘住。
  useEffect(() => {
    if (open) {
      setDraft(value || DEFAULT_CRON);
      setError(null);
    }
  }, [open, value]);

  const content = (
    <div style={{ width: 420 }}>
      <Cron
        value={draft}
        setValue={setDraft}
        locale={ZH_LOCALE}
        clearButton={false}
        humanizeLabels
        onError={(err: CronError) => setError(err ? err.description : null)}
      />
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
        {error ?? describeCron(draft)}
      </Typography.Paragraph>
      <Space style={{ justifyContent: "flex-end", width: "100%" }}>
        <Button
          size="small"
          onClick={() => {
            onChange("");
            setOpen(false);
          }}
        >
          不定时
        </Button>
        <Button size="small" onClick={() => setOpen(false)}>
          取消
        </Button>
        <Button
          size="small"
          type="primary"
          disabled={Boolean(error)}
          onClick={() => {
            onChange(draft.trim());
            setOpen(false);
          }}
        >
          确定
        </Button>
      </Space>
    </div>
  );

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomLeft"
      title="定时策略"
      content={content}
      destroyOnHidden
    >
      <Button size={size} disabled={disabled} icon={<ClockCircleOutlined />}>
        <span
          style={{
            maxWidth: 150,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {describeCron(value)}
        </span>
      </Button>
    </Popover>
  );
}
