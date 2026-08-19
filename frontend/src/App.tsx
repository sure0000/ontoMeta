import { Navigate, Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/Layout";
import { BusinessLogicCategoryPage } from "./pages/BusinessLogicCategoryPage";
import { BusinessLogicCreatePage } from "./pages/BusinessLogicCreatePage";
import { BusinessLogicDetailPage } from "./pages/BusinessLogicDetailPage";
import { BusinessLogicPage } from "./pages/BusinessLogicPage";
import { ChatBiPage } from "./pages/chat-bi";
import { DataAppEditorPage } from "./pages/DataAppEditorPage";
import { DataAppsPage } from "./pages/DataAppsPage";
import { DataAppViewPage } from "./pages/DataAppViewPage";
import { DataAppEmbedPage } from "./pages/DataAppEmbedPage";
import { DataAppPublicPage } from "./pages/DataAppPublicPage";
import { DecisionsPage } from "./pages/DecisionsPage";
import { DomainDetailPage } from "./pages/DomainDetailPage";
import { ExecutionRecordsPage } from "./pages/ExecutionRecordsPage";
import { ObjectTypeDetailPage } from "./pages/ObjectTypeDetailPage";
import { OntologyPage } from "./pages/OntologyPage";
import { PipelinesPage } from "./pages/PipelinesPage";
import { TasksPage } from "./pages/TasksPage";
import { TasksOverviewPage } from "./pages/TasksOverviewPage";
import { TaskCreatePage } from "./pages/TaskCreatePage";
import { RelationGroupDetailPage } from "./pages/RelationGroupDetailPage";
import { RelationTypeDetailPage } from "./pages/RelationTypeDetailPage";
import { SettingsPage } from "./pages/SettingsPage";
import { WorkspacePage } from "./pages/WorkspacePage";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Navigate to="/ontology" replace />} />
        <Route path="/workspace" element={<WorkspacePage />} />
        <Route path="/workspace/:domainId" element={<DomainDetailPage />} />
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
        <Route path="/ontology" element={<OntologyPage />} />
        <Route path="/ontology/relations/:relationId" element={<RelationTypeDetailPage />} />
        <Route
          path="/ontology/relation-groups/:displayName"
          element={<RelationGroupDetailPage />}
        />
        <Route path="/ontology/:objectId" element={<ObjectTypeDetailPage />} />
        <Route path="/business-logic" element={<BusinessLogicPage />} />
        <Route
          path="/business-logic/category/:categoryId"
          element={<BusinessLogicCategoryPage />}
        />
        <Route path="/business-logic/create" element={<BusinessLogicCreatePage />} />
        <Route path="/business-logic/:logicId" element={<BusinessLogicDetailPage />} />
        <Route path="/chat-bi" element={<ChatBiPage />} />
        <Route path="/decisions" element={<DecisionsPage />} />
        <Route path="/tasks" element={<TasksOverviewPage />} />
        <Route path="/tasks/create" element={<TaskCreatePage />} />
        <Route path="/tasks/:id/edit" element={<TaskCreatePage />} />
        <Route path="/tasks/orchestration" element={<PipelinesPage />} />
        {/* 保留旧路由以兼容 */}
        <Route path="/tasks/materialize" element={<TasksPage kind="materialize" />} />
        <Route path="/tasks/sync" element={<TasksPage kind="sync" />} />
        <Route path="/tasks/transform" element={<TasksPage kind="transform" />} />
        <Route path="/tasks/metric" element={<TasksPage kind="metric" />} />
        <Route path="/tasks/pipelines" element={<PipelinesPage />} />
        <Route path="/data-apps" element={<DataAppsPage />} />
        <Route path="/data-apps/:appId/edit" element={<DataAppEditorPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="/apps/:appId" element={<DataAppViewPage />} />
      <Route path="/embed/apps/:appId" element={<DataAppEmbedPage />} />
      <Route path="/public/apps/:token" element={<DataAppPublicPage />} />
    </Routes>
  );
}
