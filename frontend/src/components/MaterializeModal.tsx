import { DatabaseOutlined } from "@ant-design/icons";
import {
  Alert,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Spin,
  Tag,
  Transfer,
  message,
} from "antd";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { DataSource, MaterializationRun } from "../types";

// 目标数仓引擎（决定 DDL/ETL 方言）——与后端 app/warehouse/adapters 对齐。
const ENGINE_OPTIONS = ["hive", "doris", "starrocks", "clickhouse", "iceberg"].map(
  (e) => ({ value: e, label: e }),
);

interface Props {
  ontologyId: string;
  open: boolean;
  onClose: () => void;
}

interface FormValues {
  target_datasource_id: string;
  engine: string;
  database_prefix?: string;
}

/** 本体一键物化弹窗：选目标存储 / 引擎 / 库前缀 / 待物化实体 → 真正落库执行。
 *
 * 物化会对目标库执行 CREATE TABLE 与 INSERT OVERWRITE（覆盖写），属破坏性操作，
 * 故弹窗里明确提示，执行后就地呈现回执（分 DDL/ETL 两阶段的成功/失败）。
 */
export function MaterializeModal({ ontologyId, open, onClose }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [targetKeys, setTargetKeys] = useState<string[]>([]); // 勾选要物化的实体名
  const [allTargets, setAllTargets] = useState<{ key: string; title: string }[]>([]);
  const [result, setResult] = useState<MaterializationRun | null>(null);

  useEffect(() => {
    if (!open) return;
    setResult(null);
    setLoading(true);
    Promise.all([
      api.listDataSources(),
      api.listMaterializationContracts(ontologyId, { materialized_only: true }),
    ])
      .then(([ds, contracts]) => {
        setSources(ds);
        const targets = contracts
          .map((c) => ({
            key: c.target_name ?? c.target_id,
            title: `${c.target_display_name ?? c.target_name ?? c.target_id}（${c.target_layer}）`,
          }))
          .filter((t) => t.key);
        setAllTargets(targets);
        setTargetKeys(targets.map((t) => t.key)); // 默认全选
      })
      .catch((err) => message.error(err instanceof Error ? err.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [open, ontologyId]);

  const submit = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    if (targetKeys.length === 0) {
      message.warning("请至少勾选一个要物化的实体");
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const run = await api.materializeOntology(ontologyId, {
        target_datasource_id: values.target_datasource_id,
        engine: values.engine,
        database_prefix: values.database_prefix?.trim() || null,
        // 全选时传 null（不裁剪），否则传勾选的实体名
        selected_targets:
          targetKeys.length === allTargets.length ? null : targetKeys,
      });
      setResult(run);
      if (run.ok) message.success("物化完成：建表与数据装载均成功");
      else message.warning("物化已执行，但存在失败项，请查看回执");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "物化失败");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open={open}
      title={
        <span>
          <DatabaseOutlined /> 物化本体到目标存储
        </span>
      }
      onOk={submit}
      onCancel={onClose}
      okText={result ? "再次物化" : "执行物化"}
      okButtonProps={{ loading: running, danger: true }}
      cancelText="关闭"
      width={680}
      destroyOnClose
    >
      <Spin spinning={loading}>
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="物化会对目标库建表并覆盖写入数据（INSERT OVERWRITE）"
          description="请确认目标存储与勾选实体无误；覆盖写为破坏性操作，会重写整表/分区。"
        />
        <Form form={form} layout="vertical" initialValues={{ engine: "hive" }}>
          <Form.Item
            name="target_datasource_id"
            label="目标存储（落库到哪）"
            rules={[{ required: true, message: "请选择目标数据源" }]}
            extra="其连接串（dsn）即落库地址；需为数仓引擎，且已配置 dsn"
          >
            <Select
              placeholder="选择目标数据源"
              options={sources.map((s) => ({
                value: s.id,
                label: `${s.name}（${s.kind}）`,
              }))}
              notFoundContent={<Empty description="尚无数据源，请先在设置中创建" />}
            />
          </Form.Item>
          <Form.Item
            name="engine"
            label="目标引擎（DDL/ETL 方言）"
            rules={[{ required: true }]}
          >
            <Select options={ENGINE_OPTIONS} />
          </Form.Item>
          <Form.Item name="database_prefix" label="库名前缀（可选）" extra="如 erp → dim_erp">
            <Input placeholder="留空则用 dim / dwd / ads" allowClear />
          </Form.Item>
          <Form.Item label="待物化实体（默认全选）">
            <Transfer
              dataSource={allTargets}
              titles={["不物化", "物化"]}
              targetKeys={targetKeys}
              onChange={(keys) => setTargetKeys(keys as string[])}
              render={(item) => item.title}
              listStyle={{ width: 280, height: 220 }}
            />
          </Form.Item>
        </Form>

        {result && <MaterializeReceiptView run={result} />}
      </Spin>
    </Modal>
  );
}

/** 执行回执：分 DDL/ETL 两阶段展示成功/失败与错误。 */
function MaterializeReceiptView({ run }: { run: MaterializationRun }) {
  const r = run.receipt;
  if (!r) {
    return (
      <Alert type="error" showIcon message={`执行失败（${run.status}）`} />
    );
  }
  const phase = (label: string, p: typeof r.ddl) => {
    if (p.skipped) {
      return (
        <Descriptions.Item label={label}>
          <Tag>已跳过</Tag>
          <span className="muted">{p.skip_reason}</span>
        </Descriptions.Item>
      );
    }
    const failed = p.failed > 0 || p.error;
    return (
      <Descriptions.Item label={label}>
        <Tag color={failed ? "red" : "green"}>
          成功 {p.executed} / 失败 {p.failed} / 共 {p.total}
        </Tag>
        {p.error && <div style={{ color: "#cf1322", marginTop: 4 }}>{p.error}</div>}
      </Descriptions.Item>
    );
  };
  return (
    <>
      <Divider style={{ margin: "12px 0" }} />
      <Alert
        type={run.ok ? "success" : "warning"}
        showIcon
        style={{ marginBottom: 12 }}
        message={run.ok ? "物化成功" : "物化已执行，存在失败项"}
      />
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="目标存储">
          {r.target_datasource.name}（{r.engine}）
        </Descriptions.Item>
        {phase("建表 DDL", r.ddl)}
        {phase("数据装载 ETL", r.etl)}
        {r.unsupported && r.unsupported.length > 0 && (
          <Descriptions.Item label="未生成">
            {r.unsupported.map((u, i) => (
              <div key={i} className="muted">
                {u.target}：{u.reason}
              </div>
            ))}
          </Descriptions.Item>
        )}
      </Descriptions>
    </>
  );
}
