import { Alert, Checkbox, Divider, Form, Input, Modal, Select, Space, Tag, Typography } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import type { DatasetEntry, DerivedFieldInput, DerivedJoin, DerivedObjectCreated } from "../types";

const { Text } = Typography;

/**
 * 由数仓里的若干数据集派生一个**新粒度**的业务对象。
 *
 * 什么时候该用它：多表 join 出的宽表、按新维度汇总的表——一行代表的东西变了，那是一个
 * 新的业务概念，本体里必须有它的名字，否则下游没有东西可引用。
 *
 * 什么时候**不该**用它：搬运和 1:1 清洗。那些不改变粒度，只是同一个对象的另一个落点，
 * 在「数仓落点」里已经看得见——为它们再建一个对象就是造副本。
 *
 * 所以「粒度」在这里是必填项：它就是判据本身。连接条件也必填（每个非主表上游都要有），
 * 少一条不会报错，只会安静地把行数乘起来。
 */

interface Props {
  open: boolean;
  ontologyId: string;
  /** 从数仓落点里选中的上游，**第一个是主表**。 */
  upstreams: DatasetEntry[];
  onClose: () => void;
  onCreated: (created: DerivedObjectCreated) => void;
}

interface Column {
  name: string;
  displayName: string;
}

const LAYER_OPTIONS = [
  { value: "dwd", label: "DWD · 明细层（join 出的宽表/事实明细）" },
  { value: "dws", label: "DWS · 汇总层（按新维度聚合）" },
  { value: "dim", label: "DIM · 维度层（整合出的维度）" },
];

/** 列名 → 属性名；撞名时加上游前缀，避免提交后被后端以「字段重复」打回。 */
function uniqueProperty(column: string, entityName: string, taken: Set<string>): string {
  const base = column.toLowerCase();
  if (!taken.has(base)) return base;
  const prefixed = `${entityName}_${base}`.toLowerCase();
  if (!taken.has(prefixed)) return prefixed;
  let i = 2;
  while (taken.has(`${prefixed}_${i}`)) i += 1;
  return `${prefixed}_${i}`;
}

export function DeriveObjectModal({ open, ontologyId, upstreams, onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [grain, setGrain] = useState("");
  const [layer, setLayer] = useState("dwd");
  const [description, setDescription] = useState("");
  const [columns, setColumns] = useState<Record<string, Column[]>>({});
  const [loadingColumns, setLoadingColumns] = useState(false);
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [joins, setJoins] = useState<Record<string, DerivedJoin>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primary = upstreams[0];
  const others = useMemo(() => upstreams.slice(1), [upstreams]);

  // 列取自上游**对象的属性**：ODS 是源表的一比一镜像，DWD 落点的列也由同一个对象的
  // 属性生成，所以本体里的属性就是那张表的列——不必再去数仓 information_schema 读一遍。
  const loadColumns = useCallback(async () => {
    setLoadingColumns(true);
    try {
      const entries = await Promise.all(
        upstreams.map(async (u) => {
          try {
            const detail = await api.getObjectType(u.entity_id);
            return [
              u.ref,
              (detail.properties || []).map((p) => ({
                name: p.name,
                displayName: p.display_name || p.name,
              })),
            ] as const;
          } catch {
            return [u.ref, [] as Column[]] as const;
          }
        }),
      );
      setColumns(Object.fromEntries(entries));
    } finally {
      setLoadingColumns(false);
    }
  }, [upstreams]);

  useEffect(() => {
    if (!open || upstreams.length === 0) return;
    setError(null);
    setPicked({});
    setJoins(
      Object.fromEntries(
        upstreams.slice(1).map((u) => [
          u.ref,
          { left_ref: upstreams[0].ref, right_ref: u.ref, how: "inner", on: [{ left: "", right: "" }] },
        ]),
      ),
    );
    void loadColumns();
  }, [open, upstreams, loadColumns]);

  const fields: DerivedFieldInput[] = useMemo(() => {
    const taken = new Set<string>();
    const out: DerivedFieldInput[] = [];
    for (const upstream of upstreams) {
      for (const column of picked[upstream.ref] || []) {
        const property = uniqueProperty(column, upstream.entity_name, taken);
        taken.add(property);
        out.push({ property, from_ref: upstream.ref, from_column: column });
      }
    }
    return out;
  }, [picked, upstreams]);

  const joinsComplete = others.every((u) => {
    const join = joins[u.ref];
    return join && join.on.length > 0 && join.on.every((c) => c.left && c.right);
  });
  const canSubmit =
    Boolean(name.trim() && displayName.trim() && grain.trim()) &&
    fields.length > 0 &&
    joinsComplete;

  const updateJoin = (ref: string, patch: Partial<DerivedJoin>) =>
    setJoins((prev) => ({ ...prev, [ref]: { ...prev[ref], ...patch } }));

  const updateCondition = (ref: string, index: number, patch: { left?: string; right?: string }) =>
    setJoins((prev) => ({
      ...prev,
      [ref]: {
        ...prev[ref],
        on: prev[ref].on.map((c, i) => (i === index ? { ...c, ...patch } : c)),
      },
    }));

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createDerivedObject(ontologyId, {
        name: name.trim(),
        display_name: displayName.trim(),
        grain: grain.trim(),
        description: description.trim() || undefined,
        layer,
        upstream_refs: upstreams.map((u) => u.ref),
        joins: others.map((u) => joins[u.ref]),
        fields,
      });
      onCreated(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const columnOptions = (ref: string) =>
    (columns[ref] || []).map((c) => ({ value: c.name, label: `${c.displayName}（${c.name}）` }));

  return (
    <Modal
      title="由数据集派生业务对象"
      open={open}
      onCancel={onClose}
      onOk={handleSubmit}
      okText="创建派生对象"
      okButtonProps={{ disabled: !canSubmit, loading: submitting }}
      width={760}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="只有粒度变了才该建新对象"
        description="搬运和 1:1 清洗不改变一行代表的东西，那是同一个对象的另一个落点（在「数仓落点」里已能看见），不该在本体里多出一个实体。"
      />

      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}

      <Form layout="vertical">
        <Form.Item label="上游数据集">
          <Space orientation="vertical" size={4} style={{ width: "100%" }}>
            {upstreams.map((u, i) => (
              <Space key={u.ref} size={6}>
                <Tag color={i === 0 ? "blue" : "default"}>{i === 0 ? "主表" : "关联"}</Tag>
                <Text code>{u.physical}</Text>
                <Text type="secondary">{u.entity_display_name}</Text>
              </Space>
            ))}
          </Space>
        </Form.Item>

        {others.length > 0 && (
          <Form.Item
            label="连接条件"
            extra="每个关联上游都必须接上：少一条连接不会报错，只会安静地把行数乘起来。"
          >
            <Space orientation="vertical" size={8} style={{ width: "100%" }}>
              {others.map((u) => {
                const join = joins[u.ref];
                if (!join) return null;
                const leftCandidates = upstreams.filter((x) => x.ref !== u.ref);
                return (
                  <Space key={u.ref} wrap size={6}>
                    <Select
                      size="small"
                      style={{ width: 190 }}
                      value={join.left_ref}
                      onChange={(v) => updateJoin(u.ref, { left_ref: v })}
                      options={leftCandidates.map((x) => ({
                        value: x.ref,
                        label: x.entity_display_name,
                      }))}
                    />
                    <Select
                      size="small"
                      style={{ width: 150 }}
                      value={join.on[0]?.left}
                      placeholder="左侧字段"
                      onChange={(v) => updateCondition(u.ref, 0, { left: v })}
                      options={columnOptions(join.left_ref)}
                      showSearch
                      optionFilterProp="label"
                    />
                    <Text>=</Text>
                    <Select
                      size="small"
                      style={{ width: 150 }}
                      value={join.on[0]?.right}
                      placeholder={`${u.entity_display_name} 的字段`}
                      onChange={(v) => updateCondition(u.ref, 0, { right: v })}
                      options={columnOptions(u.ref)}
                      showSearch
                      optionFilterProp="label"
                    />
                    <Select
                      size="small"
                      style={{ width: 100 }}
                      value={join.how}
                      onChange={(v) => updateJoin(u.ref, { how: v })}
                      options={[
                        { value: "inner", label: "inner" },
                        { value: "left", label: "left" },
                      ]}
                    />
                  </Space>
                );
              })}
            </Space>
          </Form.Item>
        )}

        <Form.Item
          label={`字段（已选 ${fields.length}）`}
          extra="语义类型与物理类型从上游属性照抄，不重新猜。列名撞车时自动加上游前缀。"
        >
          <Space orientation="vertical" size={8} style={{ width: "100%" }}>
            {upstreams.map((u) => (
              <div key={u.ref}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {u.entity_display_name}
                </Text>
                {(columns[u.ref] || []).length === 0 ? (
                  <div>
                    <Text type="secondary">
                      {loadingColumns ? "读取字段中…" : "该上游没有可选字段"}
                    </Text>
                  </div>
                ) : (
                  <Checkbox.Group
                    value={picked[u.ref] || []}
                    onChange={(v) => setPicked((prev) => ({ ...prev, [u.ref]: v as string[] }))}
                    options={columnOptions(u.ref)}
                  />
                )}
              </div>
            ))}
          </Space>
        </Form.Item>

        <Divider plain>
          <Text type="secondary">新对象</Text>
        </Divider>

        <Form.Item label="中文名" required>
          <Input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="如：订单商品宽表"
          />
        </Form.Item>
        <Form.Item label="标识名" required extra="本体内唯一，用于 SQL 与物理表名">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：order_item_wide"
          />
        </Form.Item>
        <Form.Item
          label="粒度"
          required
          extra="这张表的一行代表什么。它是「该不该建新对象」的判据，必须写清楚。"
        >
          <Input
            value={grain}
            onChange={(e) => setGrain(e.target.value)}
            placeholder="如：一行 = 一张订单的一个商品行"
          />
        </Form.Item>
        <Form.Item label="落层">
          <Select value={layer} onChange={setLayer} options={LAYER_OPTIONS} />
        </Form.Item>
        <Form.Item label="说明">
          <Input.TextArea
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Form.Item>
      </Form>

      {primary && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          创建后它是本体里的普通业务对象：可物化建表，不可同步（上游在数仓里，不在源库）。
        </Text>
      )}
    </Modal>
  );
}
