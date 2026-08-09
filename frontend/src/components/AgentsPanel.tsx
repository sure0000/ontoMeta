import {
  CheckCircleOutlined,
  EditOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Cascader,
  Collapse,
  Descriptions,
  Drawer,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import { SectionCard } from "./SectionCard";
import { SpecForm } from "./artifact-spec/SpecForm";
import {
  CLEANSING_RULES,
  SPEC_FIELDS,
} from "./artifact-spec/specFields";
import { useSpecOptions } from "./artifact-spec/useSpecOptions";
import type {
  AgentKinds,
  AgentValidationIssue,
  DomainContext,
  GovernanceArtifact,
  OntologySummary,
} from "../types";

const { Text } = Typography;

const PRE_STYLE: React.CSSProperties = {
  background: "rgba(0,0,0,0.03)",
  border: "1px solid rgba(0,0,0,0.06)",
  borderRadius: 6,
  padding: "8px 12px",
  margin: 0,
  maxHeight: 320,
  overflow: "auto",
  fontSize: 12,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const KIND_LABEL: Record<string, string> = {
  sync: "数据同步 sync",
  transform: "数据加工 transform",
  metric: "指标任务 metric",
  materialize: "物化 materialize",
};

/** 中文短名，用于 kind 锁定时的面板标题（不带英文后缀）。 */
const KIND_SHORT_LABEL: Record<string, string> = {
  sync: "数据同步",
  transform: "数据加工",
  metric: "指标任务",
  materialize: "物化任务",
};

const STATUS_COLOR: Record<string, string> = {
  drafted: "default",
  validated: "blue",
  confirmed: "gold",
  executing: "processing",
  succeeded: "green",
  failed: "red",
  // Airflow DagRun 状态（live_state）
  success: "green",
  running: "processing",
  queued: "default",
  scheduled: "default",
  upstream_failed: "red",
};

// 与后端 validation._WARNING_CODES 对齐：这些是 warning 级，其余为阻断级。
const WARNING_CODES = new Set(["engine_unverified", "ontology_issue"]);

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** 用本体列表 + 域名构建级联第 1 层节点（非物化 kind 的本体→实体级联）。 */
function buildOntologyCascadeNodes(
  onts: OntologySummary[],
  doms: DomainContext[],
): MaterializeCascadeNode[] {
  const domName = (id: string) => doms.find((d) => d.id === id)?.name ?? id;
  return onts.map((o) => ({
    value: o.id,
    label: `${domName(o.domain_context_id)} v${o.version}（${o.status}）`,
    isLeaf: false,
  }));
}

/** 物化级联选择的节点。实体级联：本体→实体（value=实体名）；
 *  目标库级联：数据源→库（库层 isLeaf，需时 loadData 懒加载）。 */
interface MaterializeCascadeNode {
  value: string;
  label: string;
  children?: MaterializeCascadeNode[];
  isLeaf?: boolean;
  loading?: boolean;
}

/**
 * 非物化 kind 的实体级联配置：本体 → 实体（单选）。
 * - sync/transform 选本体对象（objectTypes），value=对象 name
 * - metric 选业务逻辑（businessLogics），value=logic id
 * 对应 SPEC_FIELDS 里那个 required 的实体字段，由级联接管后从 SpecForm 跳过。
 */
const KIND_ENTITY_CASCADE: Record<
  string,
  { fieldKey: string; label: string; source: "objectTypes" | "businessLogics" }
> = {
  sync: {
    fieldKey: "object_type",
    label: "本体 / 对象",
    source: "objectTypes",
  },
  transform: {
    fieldKey: "target_table",
    label: "本体 / 目标对象",
    source: "objectTypes",
  },
  metric: {
    fieldKey: "business_logic_id",
    label: "本体 / 业务逻辑",
    source: "businessLogics",
  },
};

/**
 * 治理智能体制品：草稿 → 校验 → 确认 → 执行。整个命名空间需 publisher 角色。
 *
 * 传入 `kind` 时化身为「某一类型任务」的专属面板（列表按该 kind 过滤、起草弹窗锁定该
 * 类型）；不传则是覆盖全部类型的通用面板。任务管理菜单的 5 个类型页正是靠此 prop 复用
 * 同一份组件。
 */
export function AgentsPanel({ kind }: { kind?: string } = {}) {
  const navigate = useNavigate();
  const [rows, setRows] = useState<GovernanceArtifact[]>([]);
  const [kinds, setKinds] = useState<AgentKinds | null>(null);
  const [loading, setLoading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [detail, setDetail] = useState<GovernanceArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 起草表单的结构化状态（取代原 intent/context JSON textarea）
  const [draftKind, setDraftKind] = useState<string>(kind ?? "metric");
  const [draftOntologyId, setDraftOntologyId] = useState<string | undefined>();
  const [specDraft, setSpecDraft] = useState<Record<string, unknown>>({});

  // 本体下拉数据（OntologySummary 自身无名字，靠 domain_context_id 关联域名做 label）
  const [ontologies, setOntologies] = useState<OntologySummary[]>([]);
  const [domains, setDomains] = useState<DomainContext[]>([]);

  // 物化任务专用：本体 → 可物化实体（业务对象 + 事实/桥表关系）的级联选择（支持搜索）。
  // value 是若干 [ontologyId, entityName] 路径；单本体绑定，实体名落 selected_targets。
  const [cascadeOptions, setCascadeOptions] = useState<MaterializeCascadeNode[]>([]);
  const [cascadeValue, setCascadeValue] = useState<string[][]>([]);
  // 实体元数据：ontologyId → 实体名 → {自动生成的表名}。用于预览将建的表。
  const [entityTable, setEntityTable] = useState<
    Record<string, Record<string, string>>
  >({});
  // 目标数据库级联：数据源 → 库（库层懒加载）。value = [datasourceId, database]。
  const [dbCascadeOptions, setDbCascadeOptions] = useState<MaterializeCascadeNode[]>(
    [],
  );
  const [dbCascadeValue, setDbCascadeValue] = useState<string[]>([]);
  const [cascadeLoading, setCascadeLoading] = useState(false);

  // 非物化 kind 的本体→实体级联（sync/transform/metric）。第 1 层=本体，第 2 层=对象/
  // 业务逻辑（懒加载）。value=[ontologyId, entityValue]，单选。
  const [entityCascadeOptions, setEntityCascadeOptions] = useState<
    MaterializeCascadeNode[]
  >([]);
  const [entityCascadeValue, setEntityCascadeValue] = useState<string[]>([]);

  const effectiveKind = kind ?? draftKind;

  const handleError = useCallback((err: unknown, fallback: string) => {
    if (err instanceof ApiError && err.status === 403) {
      setForbidden(true);
      return;
    }
    message.error(err instanceof Error ? err.message : fallback);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [artifacts, kindsOut] = await Promise.all([
        api.listArtifacts(kind ? { kind } : undefined),
        api.listAgentKinds(),
      ]);
      setRows(artifacts);
      setKinds(kindsOut);
      setForbidden(false);
    } catch (err) {
      handleError(err, "加载失败");
    } finally {
      setLoading(false);
    }
  }, [handleError, kind]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshDetail = useCallback(async (id: string) => {
    try {
      setDetail(await api.getArtifact(id));
    } catch {
      /* 详情刷新失败不打断主流程 */
    }
  }, []);

  const openCreate = useCallback(() => {
    // 重置表单到初始态，并按需拉本体下拉数据
    const startKind = kind ?? "metric";
    setDraftKind(startKind);
    setDraftOntologyId(undefined);
    setSpecDraft({});
    setCascadeValue([]);
    setDbCascadeValue([]);
    setEntityCascadeValue([]);
    setCreateOpen(true);
    if (!ontologies.length) {
      Promise.all([api.listOntologies(), api.listDomains()])
        .then(([onts, doms]) => {
          setOntologies(onts);
          setDomains(doms);
          // 非物化 kind 的实体级联第 1 层用本体列表构建（第 2 层懒加载）。
          if (KIND_ENTITY_CASCADE[startKind]) {
            setEntityCascadeOptions(buildOntologyCascadeNodes(onts, doms));
          }
        })
        .catch(() => {
          /* 下拉数据拉取失败不阻断，用户仍可手填其它字段 */
        });
    } else if (KIND_ENTITY_CASCADE[startKind] && !entityCascadeOptions.length) {
      setEntityCascadeOptions(buildOntologyCascadeNodes(ontologies, domains));
    }
    // 物化：预先把「本体 → 可物化实体」树与「数据源」拉全，使级联搜索能覆盖到叶子。
    if (startKind === "materialize") {
      if (!cascadeOptions.length) void buildMaterializeCascade();
      if (!dbCascadeOptions.length) void loadDbCascade();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    kind,
    ontologies.length,
    cascadeOptions.length,
    dbCascadeOptions.length,
    entityCascadeOptions.length,
  ]);

  /** 一次请求拿到「每域一个工作本体 → 可物化实体」的全树（后端已去重/排掉外键），
   *  拼成级联树；契约给出层与自动表名，直接展示。 */
  const buildMaterializeCascade = useCallback(async () => {
    setCascadeLoading(true);
    try {
      const { ontologies: onts } = await api.listMaterializeTargets();
      const tableMap: Record<string, Record<string, string>> = {};
      const nodes: MaterializeCascadeNode[] = onts.map((o) => {
        const perEntity: Record<string, string> = {};
        const children: MaterializeCascadeNode[] = o.entities.map((e) => {
          perEntity[e.name] = e.table;
          const kindLabel = e.kind === "relation_type" ? "关系" : "对象";
          const display = e.display_name || e.name;
          return {
            value: e.name,
            label: `${kindLabel} · ${display} → ${e.table}`,
          };
        });
        tableMap[o.ontology_id] = perEntity;
        return {
          value: o.ontology_id,
          label: `${o.domain_name} v${o.version}（${o.status}）`,
          children,
        };
      });
      setEntityTable(tableMap);
      setCascadeOptions(nodes);
    } catch {
      /* 拉取失败不阻断；级联为空时面板会提示重试 */
    } finally {
      setCascadeLoading(false);
    }
  }, []);

  /** 目标库级联的第 1 层：数据源（库层置 isLeaf=false，由 loadDbData 懒加载）。 */
  const loadDbCascade = useCallback(async () => {
    try {
      const list = await api.listDataSources();
      setDbCascadeOptions(
        list.map((d) => ({
          value: d.id,
          label: `${d.name}（${d.kind}）`,
          isLeaf: false,
        })),
      );
    } catch {
      /* 拉不到数据源不阻断 */
    }
  }, []);

  /** 展开某数据源时懒拉其库列表。 */
  const loadDbData = useCallback(async (selected: MaterializeCascadeNode[]) => {
    const target = selected[selected.length - 1];
    target.loading = true;
    try {
      const { databases } = await api.listDataSourceDatabases(target.value);
      target.children = databases.map((name) => ({
        value: name,
        label: name,
        isLeaf: true,
      }));
    } catch {
      target.children = [];
    }
    target.loading = false;
    setDbCascadeOptions((prev) => [...prev]);
  }, []);

  /** 非物化 kind 实体级联的第 2 层：展开某本体时懒拉其对象 / 业务逻辑列表。 */
  const loadEntityCascadeData = useCallback(
    async (selected: MaterializeCascadeNode[]) => {
      const target = selected[selected.length - 1];
      const cfg = KIND_ENTITY_CASCADE[effectiveKind];
      if (!cfg) return;
      target.loading = true;
      try {
        let children: MaterializeCascadeNode[];
        if (cfg.source === "objectTypes") {
          const page = await api.listObjectTypes({
            ontologyId: target.value,
            publishedOnly: false,
          });
          children = page.items.map((o) => ({
            value: o.name,
            label: o.display_name || o.name,
            isLeaf: true,
          }));
        } else {
          const page = await api.listBusinessLogics({ ontologyId: target.value });
          children = page.items.map((b) => ({
            value: b.id,
            label: b.display_name || b.name,
            isLeaf: true,
          }));
        }
        target.children = children;
      } catch {
        target.children = [];
      }
      target.loading = false;
      setEntityCascadeOptions((prev) => [...prev]);
    },
    [effectiveKind],
  );

  /** 非物化 kind 实体级联变更：[ontologyId, entityValue] →
   *  写入 draftOntologyId 与 spec 里对应的实体字段。 */
  const onEntityCascadeChange = useCallback(
    (val: string[]) => {
      setEntityCascadeValue(val);
      const cfg = KIND_ENTITY_CASCADE[effectiveKind];
      if (!cfg) return;
      if (!val || val.length === 0) {
        setDraftOntologyId(undefined);
        setSpecDraft((prev) => {
          const { [cfg.fieldKey]: _drop, ...rest } = prev;
          return rest;
        });
        return;
      }
      const [ontologyId, entityValue] = val;
      setDraftOntologyId(ontologyId);
      setSpecDraft((prev) => ({ ...prev, [cfg.fieldKey]: entityValue }));
    },
    [effectiveKind],
  );

  /** 目标库级联变更：[数据源id, 库名] → 写入 spec 的 target_datasource_id / target_database。 */
  const onDbCascadeChange = useCallback((val: string[]) => {
    setDbCascadeValue(val);
    const [datasourceId, database] = val;
    setSpecDraft((prev) => ({
      ...prev,
      target_datasource_id: datasourceId,
      target_database: database,
    }));
  }, []);

  /** 级联选择变更：单任务只绑一个本体，以最新选择所属本体为准。 */
  const onCascadeChange = useCallback(
    (paths: string[][]) => {
      if (!paths.length) {
        setCascadeValue([]);
        setDraftOntologyId(undefined);
        setSpecDraft((prev) => {
          const { selected_targets: _drop, ...rest } = prev;
          return rest;
        });
        return;
      }
      // 一个物化任务只绑定一个本体：若旧选仍在新选集里则保留，否则切到新本体。
      const onts = Array.from(new Set(paths.map((p) => p[0])));
      const nextOnt =
        draftOntologyId && onts.includes(draftOntologyId)
          ? onts.find((o) => o !== draftOntologyId) ?? draftOntologyId
          : onts[onts.length - 1];
      const kept = paths.filter((p) => p[0] === nextOnt);
      const targets = kept.filter((p) => p.length > 1).map((p) => p[1]);
      setCascadeValue(kept);
      setDraftOntologyId(nextOnt);
      setSpecDraft((prev) => ({ ...prev, selected_targets: targets }));
      if (kept.length !== paths.length) {
        message.info("一个物化任务只能绑定一个本体，已切换到新选的本体");
      }
    },
    [draftOntologyId],
  );

  /** 各 kind 必填字段的前端非空校验（对齐后端闸门 missing_required_field）。 */
  const missingRequired = (): string | null => {
    for (const f of SPEC_FIELDS[effectiveKind] ?? []) {
      if (!f.required) continue;
      const v = specDraft[f.key];
      const empty =
        v == null ||
        (typeof v === "string" && !v.trim()) ||
        (Array.isArray(v) && v.length === 0);
      if (empty) return f.label;
    }
    return null;
  };

  const create = async () => {
    const missing = missingRequired();
    if (missing) {
      message.error(`请填写：${missing}`);
      return;
    }
    // 所有制品都必须绑定本体
    if (!draftOntologyId) {
      message.error("请选择本体");
      return;
    }
    // 物化：目标数据库（数据源 + 库）必选（它们由专用级联维护，不在 SPEC_FIELDS 里）。
    if (
      effectiveKind === "materialize" &&
      (!specDraft.target_datasource_id || !specDraft.target_database)
    ) {
      message.error("请选择目标数据库（数据源 / 库）");
      return;
    }
    setSubmitting(true);
    try {
      // 名称直接用本体名称（数据域名 + 版本），不再让用户手填。
      const derivedName = draftOntologyId
        ? ontologyName(draftOntologyId)
        : undefined;
      // 所有类型统一走 context+drafter 派生路径：表单收的是 drafter 输入（对象名/业务
      // 逻辑/目标源），真正落库的 spec（sync 的 source/target、transform 的结构化清洗规则、
      // materialize/transform 的 ontology_id 等）由 drafter 派生补全。此前 sync/transform/
      // materialize 把表单原样当 spec 直填，缺 drafter 派生的必填字段，一律过不了校验闸门。
      // user_created=true：表单是用户发起，溯源标 user（区别于对话/机器起草的 machine）。
      const created = await api.draftArtifact({
        kind: effectiveKind,
        name: derivedName,
        intent: derivedName,
        ontology_id: draftOntologyId ?? null,
        context: { ontology_id: draftOntologyId, ...specDraft },
        user_created: true,
      });
      setCreateOpen(false);
      await load();
      setDetail(created);
    } catch (err) {
      handleError(err, "起草失败");
    } finally {
      setSubmitting(false);
    }
  };

  const domainName = (domainContextId: string): string => {
    const d = domains.find((x) => x.id === domainContextId);
    return d?.name ?? domainContextId;
  };

  /** 本体的展示名。本体自身无名，用其所属数据域名 + 版本作为「本体名称」。
   *  物化走级联（不预先拉 ontologies 列表），优先用级联节点 label。 */
  const ontologyName = (ontologyId: string): string => {
    const node =
      cascadeOptions.find((x) => x.value === ontologyId) ??
      entityCascadeOptions.find((x) => x.value === ontologyId);
    if (node) return node.label.replace(/（[^）]*）$/, "");
    const o = ontologies.find((x) => x.id === ontologyId);
    if (!o) return ontologyId;
    return `${domainName(o.domain_context_id)} v${o.version}`;
  };

  const runStep = async (
    step: "validate" | "confirm" | "execute",
    artifact: GovernanceArtifact,
  ) => {
    setBusy(true);
    try {
      let next: GovernanceArtifact;
      if (step === "validate") {
        next = await api.validateArtifact(artifact.id);
      } else if (step === "confirm") {
        next = await api.confirmArtifact(artifact.id);
      } else {
        next = await api.executeArtifact(artifact.id);
      }
      setDetail(next);
      await load();
      const label = { validate: "校验", confirm: "确认", execute: "执行" }[step];
      message.success(`${label}完成：${next.status}`);
    } catch (err) {
      handleError(err, "操作失败");
      void refreshDetail(artifact.id);
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnsType<GovernanceArtifact> = [
    // kind 固定时（类型专属页）隐藏冗余的「类型」列
    ...(kind
      ? []
      : [
          {
            title: "类型",
            dataIndex: "kind",
            key: "kind",
            render: (k: string) => KIND_LABEL[k] ?? k,
          } as ColumnsType<GovernanceArtifact>[number],
        ]),
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: string, row) => {
        // 后端已用 live_state 覆写 status（非终态→executing，终态→succeeded/failed），
        // 故这里直接用 status；live_state 仅用于判断是否「运行中」。
        const isLive = row.live_state?.live_state && !row.live_state?.terminal;
        return (
          <Space size={4}>
            <Tag color={STATUS_COLOR[status] ?? "default"}>
              {STATUS_LABEL[status] ?? status}
            </Tag>
            {isLive && <Tag color="blue">运行中</Tag>}
            {row.is_high_risk && <Tag color="volcano">高危</Tag>}
          </Space>
        );
      },
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      render: (v: string) => new Date(v).toLocaleString(),
    },
    {
      title: "操作",
      key: "actions",
      render: (_, row) => (
        <Button size="small" onClick={() => setDetail(row)}>
          查看
        </Button>
      ),
    },
  ];

  // 物化：据已选实体与目标库预览将自动生成的表（库.层_实体名）。
  const materializeTables: string[] =
    effectiveKind === "materialize" && draftOntologyId
      ? ((specDraft.selected_targets as string[] | undefined) ?? []).map((n) => {
          const table = entityTable[draftOntologyId!]?.[n] ?? n;
          const db = specDraft.target_database as string | undefined;
          return db ? `${db}.${table}` : table;
        })
      : [];

  return (
    <SectionCard
      title={kind ? `${KIND_SHORT_LABEL[kind] ?? kind}制品` : "治理智能体制品"}
      icon={<RobotOutlined />}
      count={rows.length}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={forbidden}
            onClick={openCreate}
          >
            新建任务
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {forbidden ? (
          <Alert
            type="error"
            showIcon
            message="需要 publisher 角色"
            description="写侧智能体会改集群、建表、执行 SQL，/api/agents 整个命名空间仅 publisher 可访问。请用 publisher 或 ADMIN Token。"
          />
        ) : null}
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
        />
      </Space>

      <Modal
        open={createOpen}
        title="新建任务"
        onOk={() => void create()}
        confirmLoading={submitting}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              制品类型
            </Text>
            <Select
              style={{ width: "100%", marginTop: 4 }}
              value={effectiveKind}
              disabled={Boolean(kind)}
              onChange={(v) => {
                setDraftKind(v);
                setSpecDraft({});
                setCascadeValue([]);
                setDbCascadeValue([]);
                setEntityCascadeValue([]);
                setDraftOntologyId(undefined);
                // 切到物化且级联树未拉时，补拉一次
                if (v === "materialize") {
                  if (!cascadeOptions.length) void buildMaterializeCascade();
                  if (!dbCascadeOptions.length) void loadDbCascade();
                }
                // 切到非物化 kind 且本体列表已就绪时，构建实体级联第 1 层
                if (KIND_ENTITY_CASCADE[v] && ontologies.length) {
                  setEntityCascadeOptions(
                    buildOntologyCascadeNodes(ontologies, domains),
                  );
                }
              }}
              options={(kinds?.all_kinds ?? Object.keys(KIND_LABEL)).map((k) => ({
                value: k,
                label:
                  (KIND_LABEL[k] ?? k) +
                  (kinds && !kinds.registered.includes(k) ? "（未实现）" : "") +
                  (kinds?.high_risk.includes(k) ? " · 高危" : ""),
              }))}
            />
          </div>

          {effectiveKind === "materialize" ? (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                本体 / 物化对象（业务对象 · 业务关系）
              </Text>
              <Cascader
                style={{ width: "100%", marginTop: 4 }}
                multiple
                showSearch
                allowClear
                loading={cascadeLoading}
                placeholder="先选本体，再选要物化的业务对象/关系（可搜索；只选本体 = 全部）"
                options={cascadeOptions}
                value={cascadeValue}
                onChange={(v) => onCascadeChange((v as string[][]) ?? [])}
              />
            </div>
          ) : KIND_ENTITY_CASCADE[effectiveKind] ? (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {KIND_ENTITY_CASCADE[effectiveKind].label}
              </Text>
              <Cascader
                style={{ width: "100%", marginTop: 4 }}
                showSearch
                allowClear
                expandTrigger="hover"
                placeholder="先选本体，再选要绑定的对象/业务逻辑（可搜索）"
                options={entityCascadeOptions}
                loadData={(opts) =>
                  loadEntityCascadeData(opts as MaterializeCascadeNode[])
                }
                value={entityCascadeValue}
                onChange={(v) =>
                  onEntityCascadeChange((v as string[]) ?? [])
                }
              />
            </div>
          ) : (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                本体
              </Text>
              <Select
                style={{ width: "100%", marginTop: 4 }}
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择要绑定的本体"
                value={draftOntologyId}
                onChange={(v) => setDraftOntologyId(v)}
                options={ontologies.map((o) => ({
                  value: o.id,
                  label: `${domainName(o.domain_context_id)} v${o.version}（${o.status}）`,
                }))}
              />
            </div>
          )}

          {effectiveKind === "materialize" && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                目标数据库（数据源 · 库）
              </Text>
              <Cascader
                style={{ width: "100%", marginTop: 4 }}
                options={dbCascadeOptions}
                loadData={(opts) => loadDbData(opts as MaterializeCascadeNode[])}
                value={dbCascadeValue}
                onChange={(v) => onDbCascadeChange((v as string[]) ?? [])}
                placeholder="先选数据源，再选目标库（引擎由数据源类型自动推定）"
              />
              {materializeTables.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    将自动生成的表（{materializeTables.length}，表名按数据标准）
                  </Text>
                  <div style={{ marginTop: 4 }}>
                    {materializeTables.map((t) => (
                      <Tag key={t} style={{ marginBottom: 4 }}>
                        {t}
                      </Tag>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          <SpecForm
            kind={effectiveKind}
            mode="manual"
            value={specDraft}
            ontologyId={draftOntologyId}
            onChange={(k, v) =>
              setSpecDraft((prev) => ({ ...prev, [k]: v }))
            }
            skipKeys={
              KIND_ENTITY_CASCADE[effectiveKind]
                ? new Set([KIND_ENTITY_CASCADE[effectiveKind].fieldKey])
                : undefined
            }
          />
        </Space>
      </Modal>

      <ArtifactDetail
        artifact={detail}
        busy={busy}
        onClose={() => setDetail(null)}
        onStep={runStep}
        onEdit={(a) => navigate(`/tasks/${a.id}/edit`)}
        ontologyName={ontologyName}
      />
    </SectionCard>
  );
}

const ORIGIN_LABEL: Record<string, string> = {
  machine: "机器创建",
  machine_edited: "机器创建·人工修改",
  user: "人工创建",
};

const STATUS_LABEL: Record<string, string> = {
  drafted: "草稿",
  validated: "已校验",
  confirmed: "已确认",
  executing: "执行中",
  succeeded: "成功",
  failed: "失败",
  // Airflow DagRun 状态
  success: "成功",
  running: "执行中",
  queued: "排队中",
  scheduled: "已调度",
  upstream_failed: "上游失败",
};

/** 详情区内统一的区块标题，避免每个区块各写一套 inline style。 */
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <Text strong style={{ fontSize: 13, marginBottom: 6, display: "block" }}>
      {children}
    </Text>
  );
}

/** spec 里这些字段是 drafter 派生的内部字段，对用户无意义，详情中不展示。 */
const INTERNAL_SPEC_KEYS = new Set([
  "source_ref",
  "source_ref_alias",
  "target_table_name",
  "source_urn",
  "source_platform",
  "source_field_refs",
  "field_mapping",
  "target_datasource_id",
  "target_database",
  "database_prefix",
  "ontology_id",
  // 以下为空或高级配置，人类可读性差
  "database_overrides",
  "table_overrides",
  "overrides",
  "preservation",
  "sync_tool",
]);

/** spec 枚举值 → 中文标签（与 specFields.ts 的 static options 同源）。 */
const SPEC_VALUE_LABELS: Record<string, string> = {
  // load_strategy / mode
  full: "全量覆盖",
  incremental: "增量追加",
  cdc: "CDC 变更捕获",
  // target_layer
  dim: "维度层 DIM",
  dwd: "明细层 DWD",
  dws: "汇总层 DWS",
  ads: "应用层 ADS",
  // execution_mode
  batch: "批处理",
  streaming: "流处理",
};

/** 把 spec 里的值渲染成可读字符串：枚举映射中文、数组用顿号拼接、对象递归提取。 */
function specValueToText(fieldKey: string, value: unknown): string {
  if (value == null || value === "") return "—";

  // 数组
  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    const items = value.map((v) => {
      // 数组元素是对象（如 cleansing_rules 的 [{rule, description}]）
      if (v != null && typeof v === "object") {
        const obj = v as Record<string, unknown>;
        if (fieldKey === "cleansing_rules") {
          const rule = obj.rule as string;
          return CLEANSING_RULES.find((r) => r.value === rule)?.label ?? rule;
        }
        // 通用：取 name / label / rule 等常见字段
        return String(obj.name ?? obj.label ?? obj.rule ?? JSON.stringify(obj));
      }
      const s = String(v);
      return SPEC_VALUE_LABELS[s] ?? s;
    });
    return items.join("、");
  }

  // 对象
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length === 0) return "—";
    return keys.map((k) => `${k}: ${specValueToText(k, obj[k])}`).join(", ");
  }

  if (typeof value === "boolean") return value ? "是" : "否";
  const s = String(value);
  return SPEC_VALUE_LABELS[s] ?? s;
}

/** 把 spec 渲染成 Descriptions 条目列表，用 SPEC_FIELDS 的 label 做列名。
 *  内部派生字段（source_ref、field_mapping 等）对用户无意义，不展示。 */
function SpecDescriptions({ kind, spec }: { kind: string; spec: Record<string, unknown> }) {
  const fields = SPEC_FIELDS[kind];
  // 数据源在 spec 里只存 id（凭据不进 spec）。详情页直接印 uuid 的话，人根本看不出
  // 这个任务落到哪个仓——排查「数据没进去」时这恰恰是第一个要确认的事。
  const { options: dataSources } = useSpecOptions({ kind: "dataSources" }, null, {});
  const resolve = (key: string, value: unknown): string => {
    if (key.endsWith("datasource_id") && typeof value === "string") {
      return dataSources.find((o) => o.value === value)?.label ?? value;
    }
    return specValueToText(key, value);
  };
  const entries: { label: string; text: string }[] = [];
  if (fields) {
    for (const f of fields) {
      if (!(f.key in spec)) continue;
      entries.push({ label: f.label, text: resolve(f.key, spec[f.key]) });
    }
  }
  // SPEC_FIELDS 未覆盖但非内部的字段（如 selected_targets），用可读 key 名展示
  const covered = new Set((fields ?? []).map((f) => f.key));
  const FRIENDLY_KEY: Record<string, string> = {
    selected_targets: "物化对象",
    object_type: "对象",
    target_table: "目标表",
    business_logic_id: "业务逻辑",
    source: "源表",
    target: "目标表",
    metric_name: "指标名称",
    display_name: "显示名称",
    expression: "口径表达式",
    object_types: "关联对象",
    subject_objects: "主体对象",
    dimension_objects: "维度对象",
    properties: "关联字段",
    group_by: "分组字段",
    filters: "过滤字段",
    inputs: "输入字段",
    notes: "备注",
    refresh_cron: "调度频率",
  };
  for (const [k, v] of Object.entries(spec)) {
    if (covered.has(k) || INTERNAL_SPEC_KEYS.has(k)) continue;
    entries.push({
      label: FRIENDLY_KEY[k] ?? k,
      text: resolve(k, v),
    });
  }
  if (!entries.length) return <Text type="secondary">无配置</Text>;
  return (
    <Descriptions size="small" column={1} bordered>
      {entries.map((e) => (
        <Descriptions.Item key={e.label} label={e.label}>
          {e.text}
        </Descriptions.Item>
      ))}
    </Descriptions>
  );
}

/** 执行回执：Airflow 链接为主，建表/作业/未支持收敛进折叠面板。 */
function ExecutionReceiptDetail({
  receipt,
  liveState,
}: {
  receipt: Record<string, unknown>;
  liveState?: { live_state: string; terminal: boolean; run_url?: string } | null;
}) {
  const batches = (receipt.batches as Record<string, unknown>[]) ?? [];
  const topRunUrl = (receipt.run_url as string | undefined) ?? liveState?.run_url;
  const topDagId = receipt.dag_id as string | undefined;
  const receiptState = (receipt.state as string | undefined) ?? liveState?.live_state;
  const tables = (receipt.tables as string[]) ?? [];
  const jobs = (receipt.jobs as string[]) ?? [];
  const unsupported = (receipt.unsupported as Record<string, string>[]) ?? [];
  const receiptError = receipt.error as string | undefined;
  const isRunning = liveState && !liveState.terminal;

  // 合并 live_state 与回执的状态：live_state 更权威（实时回读 Airflow）
  const displayState = liveState?.live_state ?? receiptState;
  const displayUrl = liveState?.run_url ?? topRunUrl;

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {/* Airflow 链接区——这是执行后用户最需要的东西 */}
      {(displayState || topDagId || batches.length > 0) && (
        <div>
          {batches.length <= 1 ? (
            <Space align="center" wrap>
              <Tag
                color={STATUS_COLOR[displayState ?? ""] ?? "default"}
                style={{ margin: 0 }}
              >
                {STATUS_LABEL[displayState ?? ""] ?? displayState ?? "—"}
              </Tag>
              {isRunning && <Tag color="processing">运行中</Tag>}
              {topDagId && <Text code style={{ fontSize: 12 }}>{topDagId}</Text>}
              {displayUrl && (
                <Button
                  size="small"
                  type="link"
                  icon={<LinkOutlined />}
                  href={displayUrl}
                  target="_blank"
                  style={{ paddingLeft: 0 }}
                >
                  在 Airflow 中查看
                </Button>
              )}
            </Space>
          ) : (
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              {batches.map((b, i) => {
                const bid = b.dag_id as string | undefined;
                const brun = b.dag_run_id as string | undefined;
                const bstate = b.state as string | undefined;
                const burl = b.run_url as string | undefined;
                const btables = (b.tables as string[]) ?? [];
                return (
                  <div
                    key={i}
                    style={{
                      border: "1px solid rgba(0,0,0,0.06)",
                      borderRadius: 6,
                      padding: "8px 12px",
                    }}
                  >
                    <Space align="center" wrap>
                      <Tag
                        color={STATUS_COLOR[bstate ?? ""] ?? "default"}
                        style={{ margin: 0 }}
                      >
                        {STATUS_LABEL[bstate ?? ""] ?? bstate ?? "—"}
                      </Tag>
                      <Text code style={{ fontSize: 12 }}>{bid}</Text>
                      {brun && (
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {brun}
                        </Text>
                      )}
                      {burl && (
                        <Button
                          size="small"
                          type="link"
                          icon={<LinkOutlined />}
                          href={burl}
                          target="_blank"
                          style={{ paddingLeft: 0 }}
                        >
                          查看
                        </Button>
                      )}
                    </Space>
                    {btables.length > 0 && (
                      <div style={{ marginTop: 6 }}>
                        {btables.map((t) => (
                          <Tag key={t} style={{ fontSize: 11, marginBottom: 2 }}>
                            {t}
                          </Tag>
                        ))}
                      </div>
                    )}
                    {Boolean(b.error) && (
                      <Alert
                        style={{ marginTop: 6 }}
                        type="error"
                        showIcon
                        message={b.error as string}
                      />
                    )}
                  </div>
                );
              })}
            </Space>
          )}
        </div>
      )}

      {receiptError && (
        <Alert type="error" showIcon message="提交错误" description={receiptError} />
      )}

      {/* 建表 / 作业 / 未支持——收敛 */}
      {(tables.length > 0 || jobs.length > 0 || unsupported.length > 0) && (
        <Collapse
          size="small"
          ghost
          items={[
            ...(tables.length > 0
              ? [{
                  key: "tables",
                  label: `建表 ${tables.length} 张`,
                  children: (
                    <Space wrap size={4}>
                      {tables.map((t) => (
                        <Tag key={t} style={{ marginBottom: 2 }}>{t}</Tag>
                      ))}
                    </Space>
                  ),
                }]
              : []),
            ...(jobs.length > 0
              ? [{
                  key: "jobs",
                  label: `搬运作业 ${jobs.length} 个`,
                  children: (
                    <Space wrap size={4}>
                      {jobs.map((j) => (
                        <Tag key={j} style={{ marginBottom: 2 }}>{j}</Tag>
                      ))}
                    </Space>
                  ),
                }]
              : []),
            ...(unsupported.length > 0
              ? [{
                  key: "unsupported",
                  label: `未支持 ${unsupported.length} 项`,
                  children: (
                    <Space direction="vertical" size={2}>
                      {unsupported.map((u, i) => (
                        <Text key={i} type="secondary" style={{ fontSize: 12 }}>
                          {u.target as string}: {u.reason as string}
                        </Text>
                      ))}
                    </Space>
                  ),
                }]
              : []),
          ]}
        />
      )}
    </Space>
  );
}

/**
 * 提交前自检（Airflow 连通/鉴权/建表连接/DAG 目录双向可见）的结论。
 *
 * 这几条是**执行会不会当场失败**的直接判据，但它们混在几百条本体存量 warning 里、
 * 又被默认折叠的「问题列表」挡住——「Airflow 尚未解析到 DAG」明明校验时就查出来了，
 * 人却要等执行失败才看见。故单独提到面板顶部。
 */
function preflightIssues(
  issues: AgentValidationIssue[] | undefined,
): AgentValidationIssue[] {
  return (issues ?? []).filter((i) => i.code.startsWith("preflight"));
}

function PreflightAlert({ issues }: { issues: AgentValidationIssue[] }) {
  if (!issues.length) return null;
  const blocked = issues.some((i) => i.code === "preflight_blocked");
  return (
    <Alert
      type={blocked ? "error" : "warning"}
      showIcon
      style={{ marginBottom: 8 }}
      message={
        blocked
          ? "提交前自检发现阻断项，执行大概率失败"
          : "提交前自检有提醒项，执行可能失败"
      }
      description={
        <Space direction="vertical" size={4} style={{ width: "100%" }}>
          {issues.map((issue, idx) => (
            <div key={`${issue.code}-${idx}`}>{issue.message}</div>
          ))}
        </Space>
      }
    />
  );
}

function IssueList({ issues }: { issues: AgentValidationIssue[] }) {
  if (!issues.length) return <Text type="success">无问题</Text>;
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {issues.map((issue, idx) => {
        const warning = WARNING_CODES.has(issue.code);
        return (
          <div key={`${issue.code}-${idx}`}>
            <Tag color={warning ? "gold" : "red"}>{warning ? "warning" : "阻断"}</Tag>
            <Text code>{issue.code}</Text> {issue.message}
            {issue.entity_name && <Text type="secondary">（{issue.entity_name}）</Text>}
          </div>
        );
      })}
    </Space>
  );
}

export function ArtifactDetail({
  artifact,
  busy,
  onClose,
  onStep,
  onEdit,
  ontologyName,
}: {
  artifact: GovernanceArtifact | null;
  busy: boolean;
  onClose: () => void;
  onStep: (
    step: "validate" | "confirm" | "execute",
    artifact: GovernanceArtifact,
  ) => void;
  /** 编辑入口：drafted/validated/failed 态可点，交给父组件跳编辑向导。不传则不显示。 */
  onEdit?: (artifact: GovernanceArtifact) => void;
  /** 本体 ID → 展示名（数据域名 + 版本）。不传则退回原始 UUID。 */
  ontologyName?: (id: string) => string;
}) {
  if (!artifact) return null;
  const report = artifact.validation_report;
  const status = artifact.status;
  const hasReceipt = Boolean(artifact.execution_receipt);
  const hasLive = Boolean(artifact.live_state?.live_state);
  const preflight = preflightIssues(report?.issues);

  return (
    <Drawer
      open={Boolean(artifact)}
      onClose={onClose}
      width={620}
      title={
        <Space>
          {KIND_LABEL[artifact.kind] ?? artifact.kind}
          <Tag color={STATUS_COLOR[status] ?? "default"}>
            {STATUS_LABEL[status] ?? status}
          </Tag>
          {artifact.is_high_risk && <Tag color="volcano">高危</Tag>}
        </Space>
      }
      extra={
        <Space>
          {onEdit && ["drafted", "validated", "failed"].includes(status) && (
            <Button
              icon={<EditOutlined />}
              disabled={busy}
              onClick={() => onEdit(artifact)}
            >
              编辑
            </Button>
          )}
          {status === "drafted" && (
            <Button
              icon={<CheckCircleOutlined />}
              loading={busy}
              onClick={() => onStep("validate", artifact)}
            >
              校验
            </Button>
          )}
          {status === "validated" && (
            <>
              <Button loading={busy} onClick={() => onStep("validate", artifact)}>
                重新校验
              </Button>
              <Button
                type="primary"
                icon={<CheckCircleOutlined />}
                loading={busy}
                onClick={() => onStep("confirm", artifact)}
              >
                确认
              </Button>
            </>
          )}
          {status === "confirmed" &&
            (preflight.length > 0 ? (
              // 自检没过还要执行，得先让人看见结论并明确点「仍然执行」。
              <Popconfirm
                title="提交前自检未通过"
                description={
                  <div style={{ maxWidth: 380 }}>
                    {preflight.map((i, idx) => (
                      <div key={idx} style={{ marginBottom: 4 }}>
                        {i.message}
                      </div>
                    ))}
                    <b>仍要执行吗？</b>
                  </div>
                }
                okText="仍然执行"
                cancelText="取消"
                onConfirm={() => onStep("execute", artifact)}
              >
                <Button
                  type="primary"
                  danger
                  icon={<ThunderboltOutlined />}
                  loading={busy}
                >
                  执行
                </Button>
              </Popconfirm>
            ) : (
              <Button
                type="primary"
                danger
                icon={<ThunderboltOutlined />}
                loading={busy}
                onClick={() => onStep("execute", artifact)}
              >
                执行
              </Button>
            ))}
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* ---- 基本信息 ---- */}
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="任务名称">{artifact.name}</Descriptions.Item>
          {artifact.intent && (
            <Descriptions.Item label="任务描述">{artifact.intent}</Descriptions.Item>
          )}
          <Descriptions.Item label="本体">
            {artifact.ontology_id
              ? (ontologyName?.(artifact.ontology_id) ?? artifact.ontology_id)
              : "—"}
          </Descriptions.Item>
          <Descriptions.Item label="来源">
            {ORIGIN_LABEL[artifact.origin] ?? artifact.origin}
          </Descriptions.Item>
        </Descriptions>

        {/* ---- 任务配置 ---- */}
        <div>
          <SectionTitle>任务配置</SectionTitle>
          <SpecDescriptions kind={artifact.kind} spec={artifact.spec ?? {}} />
        </div>

        {/* ---- 校验报告 ---- */}
        {report && (
          <div>
            <SectionTitle>
              校验报告·
              {report.blocking_count > 0 ? (
                <Text type="danger">{report.blocking_count} 项阻断</Text>
              ) : (
                <Text type="success">无阻断</Text>
              )}
            </SectionTitle>
            <PreflightAlert issues={preflight} />
            {report.dry_run_error && (
              <Alert
                style={{ marginBottom: 8 }}
                type="warning"
                showIcon
                message="dry-run 失败"
                description={report.dry_run_error}
              />
            )}
            <Collapse
              size="small"
              ghost
              defaultActiveKey={
                report.blocking_count && report.blocking_count > 0 ? ["issues"] : []
              }
              items={[
                {
                  key: "issues",
                  label: `问题列表（${report.issues?.length ?? 0}）`,
                  children: <IssueList issues={report.issues ?? []} />,
                },
                ...(report.dry_run
                  ? [{
                      key: "dry_run",
                      label: "执行计划预览",
                      children: (
                        <pre style={PRE_STYLE}>{prettyJson(report.dry_run)}</pre>
                      ),
                    }]
                  : []),
              ]}
            />
          </div>
        )}

        {/* ---- 执行结果（含 Airflow 实时状态） ---- */}
        {(hasReceipt || hasLive) && (
          <div>
            <SectionTitle>执行结果</SectionTitle>
            {hasReceipt ? (
              <ExecutionReceiptDetail
                receipt={artifact.execution_receipt as Record<string, unknown>}
                liveState={artifact.live_state}
              />
            ) : (
              /* 有 live_state 但还没回执（DAG 已触发但回执未落盘的窗口期） */
              <Space align="center">
                <Tag
                  color={
                    STATUS_COLOR[artifact.live_state!.live_state] ?? "default"
                  }
                >
                  {STATUS_LABEL[artifact.live_state!.live_state] ??
                    artifact.live_state!.live_state}
                </Tag>
                {!artifact.live_state!.terminal && (
                  <Tag color="processing">运行中</Tag>
                )}
                {artifact.live_state!.run_url && (
                  <Button
                    size="small"
                    type="link"
                    icon={<LinkOutlined />}
                    href={artifact.live_state!.run_url}
                    target="_blank"
                  >
                    在 Airflow 中查看
                  </Button>
                )}
              </Space>
            )}
          </div>
        )}

        {/* ---- 调试信息：原始 JSON，默认收起 ---- */}
        {(hasReceipt || report?.dry_run) && (
          <Collapse
            size="small"
            ghost
            items={[
              ...(hasReceipt
                ? [{
                    key: "raw_receipt",
                    label: "原始回执 JSON",
                    children: (
                      <pre style={PRE_STYLE}>
                        {prettyJson(artifact.execution_receipt)}
                      </pre>
                    ),
                  }]
                : []),
            ]}
          />
        )}
      </Space>
    </Drawer>
  );
}
