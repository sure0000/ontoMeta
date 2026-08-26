import { ArrowLeftOutlined, RobotOutlined } from "@ant-design/icons";
import { Button, Steps, Space, message } from "antd";
import { useState, useEffect, useCallback } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { SectionCard } from "../components/SectionCard";
import { TaskTypeSelector } from "../components/task-create/TaskTypeSelector";
import { TaskDataRangeSelector } from "../components/task-create/TaskDataRangeSelector";
import { TaskConfigForm, RANGE_STEP_KEYS } from "../components/task-create/TaskConfigForm";
import { TaskPreview } from "../components/task-create/TaskPreview";
import {
  pruneHiddenSpecValues,
  requiredSpecKeys,
  specDefaults,
  SPEC_FIELDS,
  SYNC_CONN_KEYS,
  SYNC_STRATEGY_SKIP_KEYS,
} from "../components/artifact-spec/specFields";
import type { OntologySummary, DomainContext } from "../types";

/** 数据范围步骤里「实体」是否必选。物化留空 = 全量，其余三类必须指到具体实体。 */
const ENTITY_REQUIRED: Record<string, string> = {
  sync: "请选择要同步的对象",
  transform: "请选择加工的目标对象",
  // 这条任务链三类口径共用（指标/标签/规则），文案只写「指标」会让人以为标签选不了。
  metric: "请选择口径（业务逻辑：指标 / 标签 / 规则）",
};

/**
 * sync 任务的步骤内容序列（比其他任务多一步：连接配置 + 同步策略分开）。
 * 索引与 currentStep 对齐；"conn"/"strategy" 是 sync 专有的两个配置子步骤。
 */
type StepContent = "type" | "range" | "conn" | "config" | "strategy" | "preview";

function buildStepContents(kind: string): StepContent[] {
  if (kind === "sync") {
    return ["type", "range", "conn", "strategy", "preview"];
  }
  return ["type", "range", "config", "preview"];
}

function buildStepItems(contents: StepContent[]) {
  const LABELS: Record<StepContent, { title: string; description: string }> = {
    type: { title: "任务类型", description: "选择要创建的任务类型" },
    range: { title: "数据范围", description: "选择本体和相关实体" },
    conn: { title: "连接配置", description: "选择源和目标数据源" },
    config: { title: "配置参数", description: "填写任务执行参数" },
    strategy: { title: "同步策略", description: "装载方式、主键与调度" },
    preview: { title: "预览确认", description: "检查配置并提交" },
  };
  return contents.map((c) => LABELS[c]);
}

/** spec → selectedEntities 的反向映射（与 handleSubmit 里的正向映射对称）。 */
function entitiesFromSpec(kind: string, spec: Record<string, unknown>): string[] {
  if (kind === "materialize") {
    const targets = spec.selected_targets;
    return Array.isArray(targets) ? targets.map(String) : [];
  }
  if (kind === "sync" && typeof spec.object_type === "string") {
    return [spec.object_type];
  }
  if (kind === "transform" && typeof spec.target_table === "string") {
    return [spec.target_table];
  }
  if (kind === "metric" && typeof spec.business_logic_id === "string") {
    return [spec.business_logic_id];
  }
  return [];
}

/**
 * 任务创建/编辑页 - 向导式分步流程
 *
 * 将复杂的级联选择和表单拆解为 4 个清晰的步骤：
 * 1. 选择任务类型
 * 2. 选择数据范围（本体 + 实体）
 * 3. 配置参数
 * 4. 预览确认
 *
 * 路由 `/tasks/create` 为创建模式；`/tasks/:id/edit` 为编辑模式（复用同一套向导，
 * 用制品现有 spec 回填各步初值，提交时调 PATCH 而非 POST /draft）。仅允许编辑
 * drafted/validated/failed 状态的制品——后端会再次校验，前端这里不重复判断。
 */
export function TaskCreatePage() {
  const navigate = useNavigate();
  const { id } = useParams();
  const isEdit = Boolean(id);
  const [searchParams] = useSearchParams();
  const initialKind = searchParams.get("kind") || "materialize";
  const requestedReturnTo = searchParams.get("returnTo");
  const returnPath =
    requestedReturnTo?.startsWith("/") && !requestedReturnTo.startsWith("//")
      ? requestedReturnTo
      : "/tasks";

  // 编辑时类型不可变，直接落到数据范围；用户也可点步骤标题跳到任一处修改。
  const [currentStep, setCurrentStep] = useState(isEdit ? 1 : 0);
  const [submitting, setSubmitting] = useState(false);

  // 表单状态
  const [kind, setKind] = useState<string>(initialKind);
  const [ontologyId, setOntologyId] = useState<string | undefined>();
  const [selectedEntities, setSelectedEntities] = useState<string[]>([]);
  // 声明了 default 的字段先落成真值：表单上写着「默认 full」，提交的就得是 full。
  // 只作初值——人改过之后以人改的为准（见 specDefaults 的说明）。
  const [specData, setSpecData] = useState<Record<string, unknown>>(() =>
    specDefaults(initialKind),
  );
  const [taskName, setTaskName] = useState<string>("");

  // 数据
  const [ontologies, setOntologies] = useState<OntologySummary[]>([]);
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([api.listOntologies(), api.listDomains()])
      .then(([onts, doms]) => {
        setOntologies(onts);
        setDomains(doms);
      })
      .catch((err) => {
        message.error(err instanceof Error ? err.message : "加载数据失败");
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  // 编辑模式：拉取现有制品，用其 kind/ontology_id/spec 回填向导各步初值。
  useEffect(() => {
    if (!id) return;
    setLoading(true);
    api
      .getArtifact(id)
      .then((artifact) => {
        setKind(artifact.kind);
        setOntologyId(artifact.ontology_id ?? undefined);
        const spec = artifact.spec ?? {};
        setSpecData(spec);
        setTaskName(artifact.name ?? "");
        setSelectedEntities(entitiesFromSpec(artifact.kind, spec));
      })
      .catch((err) => {
        message.error(err instanceof Error ? err.message : "加载任务失败");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [id]);

  const domainName = useCallback(
    (domainContextId: string): string => {
      const d = domains.find((x) => x.id === domainContextId);
      return d?.name ?? domainContextId;
    },
    [domains],
  );

  const ontologyName = useCallback(
    (id: string): string => {
      const o = ontologies.find((x) => x.id === id);
      if (!o) return id;
      return `${domainName(o.domain_context_id)} v${o.version}`;
    },
    [ontologies, domainName],
  );

  const stepContents = buildStepContents(kind);
  const lastStep = stepContents.length - 1;

  /**
   * 交给 SpecForm 的取值。**必须把「数据范围」那一步选的实体并进来**：它存在
   * `selectedEntities` 里而不在 `specData` 里，而字段级下拉（主键/增量字段/sequence 列）
   * 要按所选对象收窄候选——看不见 object_type 就只能给一个空下拉。这些键本身在
   * RANGE_STEP_KEYS 里被跳过渲染，合进来不会多出控件。
   */
  /** 实体键的唯一权威是 `selectedEntities`；表单回写时把它们剥掉，免得两处各存一份。 */
  const handleSpecChange = (next: Record<string, unknown>) => {
    const clean = { ...next };
    for (const key of RANGE_STEP_KEYS) delete clean[key];
    setSpecData(clean);
  };

  const specWithScope: Record<string, unknown> = {
    ...specData,
    ...(selectedEntities.length > 0
      ? kind === "sync"
        ? { object_type: selectedEntities[0] }
        : kind === "transform"
          ? { target_table: selectedEntities[0] }
          : kind === "metric"
            ? { business_logic_id: selectedEntities[0] }
            : { selected_targets: selectedEntities }
      : {}),
  };

  const handleNext = () => {
    const content = stepContents[currentStep];
    if (content === "type" && !kind) {
      message.error("请选择任务类型");
      return;
    }
    if (content === "range") {
      if (!ontologyId) {
        message.error("请选择本体");
        return;
      }
      const entityHint = ENTITY_REQUIRED[kind];
      if (entityHint && selectedEntities.length === 0) {
        message.error(entityHint);
        return;
      }
    }
    if (content === "conn") {
      // 连接步骤：校验 source + target 数据源
      const connRequired = requiredSpecKeys(kind, RANGE_STEP_KEYS).filter((f) =>
        SYNC_CONN_KEYS.has(f.key),
      );
      const missing = connRequired.filter((f) => {
        const v = specData[f.key];
        return v == null || v === "" || (Array.isArray(v) && v.length === 0);
      });
      if (missing.length > 0) {
        message.error(`请填写：${missing.map((f) => f.label).join("、")}`);
        return;
      }
    }
    if (content === "config") {
      const missing = requiredSpecKeys(kind, RANGE_STEP_KEYS, specData).filter((f) => {
        const v = specData[f.key];
        return v == null || v === "" || (Array.isArray(v) && v.length === 0);
      });
      if (missing.length > 0) {
        message.error(`请填写必填项：${missing.map((f) => f.label).join("、")}`);
        return;
      }
    }
    setCurrentStep((prev) => Math.min(prev + 1, lastStep));
  };

  const handlePrev = () => {
    setCurrentStep((prev) => Math.max(prev - 1, 0));
  };

  const validateAll = (): boolean => {
    if (!ontologyId) {
      setCurrentStep(1);
      message.error("请选择本体");
      return false;
    }
    const entityHint = ENTITY_REQUIRED[kind];
    if (entityHint && selectedEntities.length === 0) {
      setCurrentStep(1);
      message.error(entityHint);
      return false;
    }
    // 校验所有 required 字段（不论当前在哪步）
    const allSkipKeys = RANGE_STEP_KEYS;
    const missing = requiredSpecKeys(kind, allSkipKeys, specData).filter((field) => {
      const value = specData[field.key];
      return value == null || value === "" || (Array.isArray(value) && value.length === 0);
    });
    if (missing.length > 0) {
      // 把用户带回第一个含缺失字段的步骤
      const connIdx = stepContents.indexOf("conn");
      const configIdx = stepContents.indexOf("config");
      const strategyIdx = stepContents.indexOf("strategy");
      const connMissing = missing.some((f) => SYNC_CONN_KEYS.has(f.key));
      if (connMissing && connIdx >= 0) {
        setCurrentStep(connIdx);
      } else if (configIdx >= 0) {
        setCurrentStep(configIdx);
      } else if (strategyIdx >= 0) {
        setCurrentStep(strategyIdx);
      }
      message.error(`请填写必填项：${missing.map((field) => field.label).join("、")}`);
      return false;
    }
    return true;
  };

  const handleSubmit = async () => {
    if (!validateAll() || !ontologyId) return;

    setSubmitting(true);
    try {
      // 构建 context（各类型任务的 drafter 输入格式）。
      // 剔除当前不可见字段：先选 CDC 填了 sequence 列、又改回全量，那个值留在这里会
      // 真的进建表语句——「确认的是全量、建出来的是 CDC 表」。
      const context: Record<string, unknown> = {
        ontology_id: ontologyId,
        ...pruneHiddenSpecValues(kind, specData),
      };

      // 添加选中的实体
      if (kind === "materialize" && selectedEntities.length > 0) {
        context.selected_targets = selectedEntities;
      } else if (kind === "sync" && selectedEntities.length > 0) {
        context.object_type = selectedEntities[0];
      } else if (kind === "transform" && selectedEntities.length > 0) {
        context.target_table = selectedEntities[0];
      } else if (kind === "metric" && selectedEntities.length > 0) {
        context.business_logic_id = selectedEntities[0];
      }

      // 留空则不传名字，交后端按 spec 派生并去重。此前留空是拿**本体名**当任务名，
      // 于是同一本体建的每个任务都同名（且「任务名称」与「本体」两行一模一样）。
      const explicitName = taskName.trim() || undefined;

      if (isEdit && id) {
        // 编辑走 PATCH + drafter 重派生；后端会把状态打回 drafted 并清空旧校验。
        await api.updateArtifact(id, {
          name: explicitName,
          intent: explicitName,
          ontology_id: ontologyId,
          context,
        });
        message.success("任务已更新，已回到草稿状态，请重新校验");
      } else {
        await api.draftArtifact({
          kind,
          name: explicitName,
          intent: explicitName,
          ontology_id: ontologyId,
          context,
          user_created: true,
        });
        message.success("任务创建成功");
      }
      navigate(returnPath);
    } catch (err) {
      message.error(err instanceof Error ? err.message : isEdit ? "更新失败" : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const steps = buildStepItems(stepContents);

  return (
    <PageContainer>
      <PageHeader
        icon={<RobotOutlined />}
        title={isEdit ? "编辑任务" : "创建任务"}
        description={
          isEdit
            ? "修改任务配置。保存后任务将回到草稿状态，需重新校验、确认、执行。"
            : "通过向导式流程，分步配置并创建数据作业任务。"
        }
        extra={
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(returnPath)}>
            {returnPath === "/tasks" ? "返回任务列表" : "返回 Data Agent"}
          </Button>
        }
      />

      <SectionCard title={isEdit ? "编辑向导" : "创建向导"}>
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Steps
            current={currentStep}
            items={steps}
            onChange={isEdit ? setCurrentStep : undefined}
          />

          <div style={{ minHeight: 400 }}>
            {stepContents[currentStep] === "type" && (
              <TaskTypeSelector
                value={kind}
                onChange={(next) => {
                  setKind(next);
                  // 换任务类型 = 换一整套字段，旧类型填的值不该留在新表单里；
                  // 新类型自己的声明默认值要跟着落成真值。
                  setSpecData(specDefaults(next));
                  setSelectedEntities([]);
                }}
                disabled={loading || isEdit}
              />
            )}

            {stepContents[currentStep] === "range" && (
              <TaskDataRangeSelector
                kind={kind}
                ontologies={ontologies}
                domains={domains}
                ontologyId={ontologyId}
                selectedEntities={selectedEntities}
                onOntologyChange={setOntologyId}
                onEntitiesChange={setSelectedEntities}
                loading={loading}
              />
            )}

            {/* sync 专属：连接配置步骤（只显示 source + target 数据源）*/}
            {stepContents[currentStep] === "conn" && (
              <TaskConfigForm
                kind={kind}
                ontologyId={ontologyId}
                value={specWithScope}
                onChange={handleSpecChange}
                name={taskName}
                onNameChange={setTaskName}
                namePlaceholder="留空则按配置自动命名（重名会自动加序号）"
                extraSkipKeys={
                  new Set(
                    (SPEC_FIELDS[kind] ?? [])
                      .map((f) => f.key)
                      .filter((k) => !SYNC_CONN_KEYS.has(k)),
                  )
                }
                showNameInput={false}
              />
            )}

            {/* 普通配置步骤（非 sync）*/}
            {stepContents[currentStep] === "config" && (
              <TaskConfigForm
                kind={kind}
                ontologyId={ontologyId}
                value={specWithScope}
                onChange={handleSpecChange}
                name={taskName}
                onNameChange={setTaskName}
                namePlaceholder="留空则按配置自动命名（重名会自动加序号）"
              />
            )}

            {/* sync 专属：同步策略步骤（跳过连接配置字段）*/}
            {stepContents[currentStep] === "strategy" && (
              <TaskConfigForm
                kind={kind}
                ontologyId={ontologyId}
                value={specWithScope}
                onChange={handleSpecChange}
                name={taskName}
                onNameChange={setTaskName}
                namePlaceholder="留空则按配置自动命名（重名会自动加序号）"
                extraSkipKeys={SYNC_STRATEGY_SKIP_KEYS}
                showNameInput={true}
              />
            )}

            {stepContents[currentStep] === "preview" && (
              <TaskPreview
                kind={kind}
                ontologyId={ontologyId}
                ontologyName={ontologyId ? ontologyName(ontologyId) : ""}
                taskName={taskName}
                selectedEntities={selectedEntities}
                specData={specWithScope}
              />
            )}
          </div>

          <Space style={{ width: "100%", justifyContent: "space-between" }}>
            <Button onClick={handlePrev} disabled={currentStep === 0}>
              上一步
            </Button>

            <Space>
              <Button onClick={() => navigate(returnPath)}>取消</Button>

              {isEdit && (
                <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                  保存修改
                </Button>
              )}

              {!isEdit && currentStep < lastStep ? (
                <Button type="primary" onClick={handleNext}>
                  下一步
                </Button>
              ) : !isEdit ? (
                <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                  创建任务
                </Button>
              ) : null}
            </Space>
          </Space>
        </Space>
      </SectionCard>
    </PageContainer>
  );
}
