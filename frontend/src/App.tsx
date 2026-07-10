import { Navigate, Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/Layout";
import { BusinessLogicCategoryPage } from "./pages/BusinessLogicCategoryPage";
import { BusinessLogicCreatePage } from "./pages/BusinessLogicCreatePage";
import { BusinessLogicDetailPage } from "./pages/BusinessLogicDetailPage";
import { BusinessLogicPage } from "./pages/BusinessLogicPage";
import { ChatBiPage } from "./pages/ChatBiPage";
import { DomainDetailPage } from "./pages/DomainDetailPage";
import { ExecutionRecordsPage } from "./pages/ExecutionRecordsPage";
import { ObjectTypeDetailPage } from "./pages/ObjectTypeDetailPage";
import { OntologyPage } from "./pages/OntologyPage";
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
        <Route path="/workspace/:domainId/relations/:relationId" element={<RelationTypeDetailPage />} />
        <Route path="/ontology" element={<OntologyPage />} />
        <Route path="/ontology/relations/:relationId" element={<RelationTypeDetailPage />} />
        <Route path="/ontology/:objectId" element={<ObjectTypeDetailPage />} />
        <Route path="/business-logic" element={<BusinessLogicPage />} />
        <Route path="/business-logic/category/:categoryId" element={<BusinessLogicCategoryPage />} />
        <Route path="/business-logic/create" element={<BusinessLogicCreatePage />} />
        <Route path="/business-logic/:logicId" element={<BusinessLogicDetailPage />} />
        <Route path="/chat-bi" element={<ChatBiPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
