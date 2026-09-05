/**
 * 交互式建数流程的**网页表单**：通用 Agent 没有原生问答工具时的兜底渲染面。
 *
 * Agent 调 `open_task_form` 发一个链接，用户在这里把这一环填完提交，Agent 用
 * `wait_task_form` 取回填值继续。字段、候选、预填值都由服务端实时算（与对话里的宿主表单、
 * 与 Data Agent 的向导同一份定义），页面只负责渲染和回填——这里不该出现第二套"该问什么"。
 */
import { CheckCircleOutlined, RobotOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Descriptions,
  Radio,
  Result,
  Select,
  Skeleton,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../../api";
import { CronPicker } from "../../components/CronPicker";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";
import { SectionCard } from "../../components/SectionCard";
import type { McpFlowFormField, McpFlowFormState } from "../../types";

const { Text, Paragraph } = Typography;

function FieldControl({
  field,
  value,
  onChange,
}: {
  field: McpFlowFormField;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const options = field.options.map((option) => ({
    label: option.detail ? `${option.label}（${option.detail}）` : option.label,
    value: option.value,
    disabled: option.disabled,
  }));
  if (field.type === "multiselect") {
    return (
      <Select
        mode="multiple"
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder={field.placeholder || "可多选"}
        options={options}
        value={(Array.isArray(value) ? value : value ? [value] : []) as string[]}
        onChange={onChange}
      />
    );
  }
  if (field.type === "radio" && options.length <= 4) {
    return (
      <Radio.Group
        options={options}
        value={value as string}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  if (options.length > 0) {
    return (
      <Select
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder={field.placeholder || "请选择"}
        options={options}
        value={(value as string) || undefined}
        onChange={onChange}
      />
    );
  }
  if (field.type === "cron") {
    return <CronPicker value={(value as string) || ""} onChange={onChange} size="middle" />;
  }
  if (field.type === "number") {
    return (
      <InputNumber
        style={{ width: "100%" }}
        placeholder={field.placeholder}
        value={value as number}
        onChange={onChange}
      />
    );
  }
  if (field.type === "boolean") {
    return (
      <Checkbox checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)}>
        是
      </Checkbox>
    );
  }
  if (field.type === "date") {
    return <DatePicker style={{ width: "100%" }} onChange={(_, text) => onChange(text)} />;
  }
  if (field.type === "textarea") {
    return (
      <Input.TextArea
        autoSize={{ minRows: 2, maxRows: 6 }}
        placeholder={field.placeholder}
        value={(value as string) || ""}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <Input
      placeholder={field.placeholder}
      value={(value as string) || ""}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function TaskFormPage() {
  const { formId = "" } = useParams();
  const [state, setState] = useState<McpFlowFormState | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  /** 服务端给的 value 就是这一格的当前取值（含系统预填），页面不另设默认值。 */
  const adopt = useCallback((next: McpFlowFormState) => {
    setState(next);
    const seed: Record<string, unknown> = {};
    for (const field of next.form?.fields ?? []) {
      if (field.value !== null && field.value !== undefined) seed[field.key] = field.value;
    }
    setValues(seed);
  }, []);

  useEffect(() => {
    if (!formId) return;
    setLoading(true);
    api
      .getMcpFlowForm(formId)
      .then(adopt)
      .catch((err: unknown) =>
        message.error(err instanceof Error ? err.message : "读取表单失败"),
      )
      .finally(() => setLoading(false));
  }, [formId, adopt]);

  const submit = async () => {
    setSubmitting(true);
    try {
      // 执行审查这一步才是"确认"：把页面上显示的方案指纹一起回传，服务端据此判断
      // 提交前方案有没有变过——变了就退回重看，不拿旧确认放行。
      const result = await api.submitMcpFlowForm(formId, values, {
        confirm: isReview,
        planDigest: state?.form?.submit_value,
      });
      adopt(result);
      if (result.accepted) {
        message.success("已确认，回到对话继续");
      } else {
        // 没受理不是失败：还缺参数（比如刚把装载方式改成增量），或方案变了要重看。
        message.warning(result.reason || "还没填完");
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const isReview = state?.stage === "review";
  const review = state?.review;
  const fields = state?.form?.fields ?? [];

  return (
    <PageContainer>
      <PageHeader icon={<RobotOutlined />} title="任务参数确认" />
      {loading ? (
        <SectionCard title="加载中">
          <Skeleton active paragraph={{ rows: 6 }} />
        </SectionCard>
      ) : state?.status === "submitted" ? (
        <Result
          icon={<CheckCircleOutlined />}
          status="success"
          title="已确认"
          subTitle="回到对话里继续——Agent 会拿着这份方案往下走。"
        />
      ) : state?.status === "expired" ? (
        <Result status="warning" title="表单已过期" subTitle="让 Agent 重新发一张。" />
      ) : !state?.form ? (
        <Result
          status="info"
          title="这张表单没有可填的内容了"
          subTitle={state?.reason || "参数可能已在别处填齐，回到对话确认当前进度。"}
        />
      ) : (
        <SectionCard
          title={state.form.title}
          extra={
            <Text type="secondary">{isReview ? "执行前唯一一次确认" : "系统定不下来的参数"}</Text>
          }
        >
          {review && (
            <>
              {review.stale_confirmation && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="方案已变，请重新核对"
                  description="上一次确认对应的是改动前的方案，已作废。"
                />
              )}
              <Alert
                type={review.blocking_count ? "error" : "info"}
                showIcon
                style={{ marginBottom: 16 }}
                message={
                  review.blocking_count
                    ? `${review.name} · ${review.blocking_count} 条阻断项，修掉才能执行`
                    : review.name
                }
                description={
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    {review.notes.map((note) => (
                      <Text key={note}>{note}</Text>
                    ))}
                    {review.blocking_issues.map((issue, index) => (
                      <Text key={`${issue.code}-${index}`} type="danger">
                        {issue.message}
                      </Text>
                    ))}
                  </Space>
                }
              />
              {/* 摆的是 Drafter 派生的那份 Spec：审查要审的正是"我填的东西到了执行期会变成什么"。 */}
              <Descriptions
                size="small"
                bordered
                column={1}
                style={{ marginBottom: 16 }}
                items={review.plan.map((row) => ({
                  key: row.key,
                  label: row.label,
                  children: row.value,
                }))}
              />
            </>
          )}
          {state.form.missing?.length ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="还缺几项"
              description="改了装载方式之后会长出新的必填项——填齐才能继续。"
            />
          ) : null}
          <Paragraph type="secondary" style={{ fontSize: 12 }}>
            {isReview
              ? "下面是这次任务的全部参数，标了「系统预填」的是按本体、契约或默认值推导的；改任何一项都会重算上面的方案。"
              : "只列了系统定不下来的项；其余参数已推导好，会在最后的执行审查里一次给你核对。"}
          </Paragraph>
          <Form layout="vertical">
            {fields.map((field) => (
              <Form.Item
                key={field.key}
                required={field.required}
                validateStatus={field.error ? "error" : undefined}
                help={field.error || field.help || field.note}
                label={
                  <Space size={6}>
                    <span>{field.label}</span>
                    {field.auto && <Tag color="blue">系统预填</Tag>}
                    {field.options_truncated && (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        共 {field.options_total} 个候选
                      </Text>
                    )}
                  </Space>
                }
              >
                <FieldControl
                  field={field}
                  value={values[field.key]}
                  onChange={(next) =>
                    setValues((current) => ({ ...current, [field.key]: next }))
                  }
                />
              </Form.Item>
            ))}
            <Button type="primary" onClick={() => void submit()} loading={submitting}>
              {isReview ? "确认执行方案" : "提交"}
            </Button>
          </Form>
        </SectionCard>
      )}
    </PageContainer>
  );
}
