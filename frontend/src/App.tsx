import { lazy, Suspense, type ComponentType } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AppLayout } from "./components/Layout";
import { PageSkeleton } from "./components/PageSkeleton";

type PageModule = Record<string, ComponentType<any>>;

/** Load route pages on demand so the initial shell does not import every feature. */
function lazyPage(loader: () => Promise<PageModule>, exportName: string) {
  return lazy(async () => {
    const module = await loader();
    const Page = module[exportName];
    if (!Page) {
      throw new Error(`Route module does not export ${exportName}`);
    }
    return { default: Page };
  });
}

const BusinessLogicCategoryPage = lazyPage(
  () => import("./pages/BusinessLogicCategoryPage"),
  "BusinessLogicCategoryPage",
);
const BusinessLogicCreatePage = lazyPage(
  () => import("./pages/BusinessLogicCreatePage"),
  "BusinessLogicCreatePage",
);
const BusinessLogicDetailPage = lazyPage(
  () => import("./pages/BusinessLogicDetailPage"),
  "BusinessLogicDetailPage",
);
const BusinessLogicPage = lazyPage(() => import("./pages/BusinessLogicPage"), "BusinessLogicPage");
const DataAppEditorPage = lazyPage(() => import("./pages/DataAppEditorPage"), "DataAppEditorPage");
const DataAppsPage = lazyPage(() => import("./pages/DataAppsPage"), "DataAppsPage");
const DataAppViewPage = lazyPage(() => import("./pages/DataAppViewPage"), "DataAppViewPage");
const DataAppEmbedPage = lazyPage(() => import("./pages/DataAppEmbedPage"), "DataAppEmbedPage");
const DataAppPublicPage = lazyPage(() => import("./pages/DataAppPublicPage"), "DataAppPublicPage");
const DomainDetailPage = lazyPage(() => import("./pages/DomainDetailPage"), "DomainDetailPage");
const ExecutionRecordsPage = lazyPage(
  () => import("./pages/ExecutionRecordsPage"),
  "ExecutionRecordsPage",
);
const LineageSupplementPage = lazyPage(
  () => import("./pages/LineageSupplementPage"),
  "LineageSupplementPage",
);
const ReviewWorkbenchPage = lazyPage(
  () => import("./pages/ReviewWorkbenchPage"),
  "ReviewWorkbenchPage",
);
const ObjectTypeDetailPage = lazyPage(
  () => import("./pages/ObjectTypeDetailPage"),
  "ObjectTypeDetailPage",
);
const OntologyPage = lazyPage(() => import("./pages/OntologyPage"), "OntologyPage");
const TasksOverviewPage = lazyPage(() => import("./pages/TasksOverviewPage"), "TasksOverviewPage");
const TaskCreatePage = lazyPage(() => import("./pages/TaskCreatePage"), "TaskCreatePage");
const RelationGroupDetailPage = lazyPage(
  () => import("./pages/RelationGroupDetailPage"),
  "RelationGroupDetailPage",
);
const RelationTypeDetailPage = lazyPage(
  () => import("./pages/RelationTypeDetailPage"),
  "RelationTypeDetailPage",
);
const SegmentsPage = lazyPage(() => import("./pages/SegmentsPage"), "SegmentsPage");
const SegmentDetailPage = lazyPage(() => import("./pages/SegmentDetailPage"), "SegmentDetailPage");
const SettingsPage = lazyPage(() => import("./pages/SettingsPage"), "SettingsPage");
const WorkspacePage = lazyPage(() => import("./pages/WorkspacePage"), "WorkspacePage");
const MonitoringPage = lazyPage(
  () => import("./pages/agent-access/MonitoringPage"),
  "MonitoringPage",
);
const ServicePage = lazyPage(() => import("./pages/agent-access/ServicePage"), "ServicePage");
const SkillsPage = lazyPage(() => import("./pages/agent-access/SkillsPage"), "SkillsPage");
const TaskFormPage = lazyPage(() => import("./pages/agent-access/TaskFormPage"), "TaskFormPage");
const TokensPage = lazyPage(() => import("./pages/agent-access/TokensPage"), "TokensPage");
const ToolsPage = lazyPage(() => import("./pages/agent-access/ToolsPage"), "ToolsPage");

export default function App() {
  // 路由级边界：一个页面崩掉时外壳（侧边栏/导航）还在，用户能自己走开，而不是白屏。
  // 用 pathname 当 resetKey，换页即自动清掉上一页的错误状态。
  const { pathname } = useLocation();
  return (
    <ErrorBoundary resetKey={pathname}>
      <Suspense fallback={<PageSkeleton full />}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Navigate to="/ontology" replace />} />
          <Route path="/workspace" element={<WorkspacePage />} />
          <Route path="/workspace/:domainId" element={<DomainDetailPage />} />
          <Route path="/workspace/:domainId/review" element={<ReviewWorkbenchPage />} />
          <Route path="/workspace/:domainId/executions" element={<ExecutionRecordsPage />} />
          <Route path="/workspace/:domainId/objects/:objectId" element={<ObjectTypeDetailPage />} />
          <Route
            path="/workspace/:domainId/relations/:relationId"
            element={<RelationTypeDetailPage />}
          />
          <Route
            path="/workspace/:domainId/relation-groups/:displayName"
            element={<RelationGroupDetailPage />}
          />
          <Route path="/workspace/:domainId/segments" element={<SegmentsPage />} />
          <Route path="/segments/:id" element={<SegmentDetailPage />} />
          <Route path="/ontology" element={<OntologyPage />} />
          <Route path="/ontology/relations/:relationId" element={<RelationTypeDetailPage />} />
          <Route
            path="/ontology/relation-groups/:displayName"
            element={<RelationGroupDetailPage />}
          />
          <Route path="/ontology/:objectId" element={<ObjectTypeDetailPage />} />
          <Route path="/lineage-supplement" element={<LineageSupplementPage />} />
          <Route path="/business-logic" element={<BusinessLogicPage />} />
          <Route
            path="/business-logic/category/:categoryId"
            element={<BusinessLogicCategoryPage />}
          />
          <Route path="/business-logic/create" element={<BusinessLogicCreatePage />} />
          <Route path="/business-logic/:logicId" element={<BusinessLogicDetailPage />} />
          <Route path="/tasks" element={<TasksOverviewPage />} />
          <Route path="/tasks/create" element={<TaskCreatePage />} />
          <Route path="/tasks/:id/edit" element={<TaskCreatePage />} />
          <Route path="/data-apps" element={<DataAppsPage />} />
          <Route path="/data-apps/:appId/edit" element={<DataAppEditorPage />} />
          <Route path="/agent-access" element={<Navigate to="/agent-access/service" replace />} />
          <Route path="/agent-access/service" element={<ServicePage />} />
          <Route path="/agent-access/tools" element={<ToolsPage />} />
          <Route path="/agent-access/monitoring" element={<MonitoringPage />} />
          <Route path="/agent-access/skills" element={<SkillsPage />} />
          <Route path="/agent-access/task-form/:formId" element={<TaskFormPage />} />
          <Route path="/agent-access/tokens" element={<TokensPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="/apps/:appId" element={<DataAppViewPage />} />
        <Route path="/embed/apps/:appId" element={<DataAppEmbedPage />} />
        <Route path="/public/apps/:token" element={<DataAppPublicPage />} />
      </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}
