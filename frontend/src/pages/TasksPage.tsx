import { RobotOutlined } from "@ant-design/icons";
import { AgentsPanel } from "../components/AgentsPanel";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";

/** 各任务类型的页面标题与副标题。 */
const KIND_META: Record<string, { title: string; description: string }> = {
  materialize: {
    title: "物化任务",
    description: "把本体正向物化到目标存储（建表 + 灌数）。",
  },
  sync: {
    title: "数据同步",
    description: "把源库数据同步到目标存储。",
  },
  transform: {
    title: "数据加工",
    description: "对已入仓数据做清洗、加工与转换。",
  },
  metric: {
    title: "指标任务",
    description: "基于本体生成指标聚合表。",
  },
};

/**
 * 某一类型任务的管理页。手动起草或 Data Agent 对话创建的任务都落在同一张
 * governance_artifacts 表，这里按 kind 过滤统一呈现，可查看、校验、确认、执行与追踪。
 * 逻辑全在 AgentsPanel 里，本页只是薄壳 + 类型化的页头。
 */
export function TasksPage({ kind }: { kind: string }) {
  const meta = KIND_META[kind] ?? { title: "任务", description: "" };
  return (
    <PageContainer>
      <PageHeader
        icon={<RobotOutlined />}
        title={meta.title}
        description={`${meta.description} 手动起草与 Data Agent 创建的任务在此统一管理。`}
      />
      <AgentsPanel kind={kind} />
    </PageContainer>
  );
}
