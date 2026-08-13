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
import { requiredSpecKeys } from "../components/artifact-spec/specFields";
import type { OntologySummary, DomainContext } from "../types";

/** 数据范围步骤里「实体」是否必选。物化留空 = 全量，其余三类必须指到具体实体。 */
const ENTITY_REQUIRED: Record<string, string> = {
  sync: "请选择要同步的对象",
  transform: "请选择加工的目标对象",
  // 这条任务链三类口径共用（指标/标签/规则），文案只写「指标」会让人以为标签选不了。
  metric: "请选择口径（业务逻辑：指标 / 标签 / 规则）",
};

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

  const [currentStep, setCurrentStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);

  // 表单状态
  const [kind, setKind] = useState<string>(initialKind);
  const [ontologyId, setOntologyId] = useState<string | undefined>();
  const [selectedEntities, setSelectedEntities] = useState<string[]>([]);
  const [specData, setSpecData] = useState<Record<string, unknown>>({});
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

  const handleNext = () => {
    // 步骤间校验。此前只拦了「没选本体」，实体与必填参数一路放行到提交，
    // 后端 drafter 才抛 ValueError——错误来得晚且是英文键名，人看不懂要补什么。
    if (currentStep === 0 && !kind) {
      message.error("请选择任务类型");
      return;
    }
    if (currentStep === 1) {
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
    if (currentStep === 2) {
      const missing = requiredSpecKeys(kind, RANGE_STEP_KEYS).filter((f) => {
        const v = specData[f.key];
        return v == null || v === "" || (Array.isArray(v) && v.length === 0);
      });
      if (missing.length > 0) {
        message.error(`请填写必填项：${missing.map((f) => f.label).join("、")}`);
        return;
      }
    }
    setCurrentStep((prev) => Math.min(prev + 1, 3));
  };

  const handlePrev = () => {
    setCurrentStep((prev) => Math.max(prev - 1, 0));
  };

  const handleSubmit = async () => {
    if (!ontologyId) {
      message.error("请选择本体");
      return;
    }

    setSubmitting(true);
    try {
      // 构建 context（各类型任务的 drafter 输入格式）
      const context: Record<string, unknown> = {
        ontology_id: ontologyId,
        ...specData,
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
      navigate("/tasks");
    } catch (err) {
      message.error(err instanceof Error ? err.message : isEdit ? "更新失败" : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const steps = [
    {
      title: "任务类型",
      description: "选择要创建的任务类型",
    },
    {
      title: "数据范围",
      description: "选择本体和相关实体",
    },
    {
      title: "配置参数",
      description: "填写任务执行参数",
    },
    {
      title: "预览确认",
      description: "检查配置并提交",
    },
  ];

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
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate("/tasks")}>
            返回任务列表
          </Button>
        }
      />

      <SectionCard title={isEdit ? "编辑向导" : "创建向导"}>
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Steps current={currentStep} items={steps} />

          <div style={{ minHeight: 400 }}>
            {currentStep === 0 && (
              <TaskTypeSelector value={kind} onChange={setKind} disabled={loading || isEdit} />
            )}

            {currentStep === 1 && (
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

            {currentStep === 2 && (
              <TaskConfigForm
                kind={kind}
                ontologyId={ontologyId}
                value={specData}
                onChange={setSpecData}
                name={taskName}
                onNameChange={setTaskName}
                namePlaceholder="留空则按配置自动命名（重名会自动加序号）"
              />
            )}

            {currentStep === 3 && (
              <TaskPreview
                kind={kind}
                ontologyId={ontologyId}
                ontologyName={ontologyId ? ontologyName(ontologyId) : ""}
                taskName={taskName}
                selectedEntities={selectedEntities}
                specData={specData}
              />
            )}
          </div>

          <Space style={{ width: "100%", justifyContent: "space-between" }}>
            <Button onClick={handlePrev} disabled={currentStep === 0}>
              上一步
            </Button>

            <Space>
              <Button onClick={() => navigate("/tasks")}>取消</Button>

              {currentStep < 3 ? (
                <Button type="primary" onClick={handleNext}>
                  下一步
                </Button>
              ) : (
                <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                  {isEdit ? "保存修改" : "创建任务"}
                </Button>
              )}
            </Space>
          </Space>
        </Space>
      </SectionCard>
    </PageContainer>
  );
}
