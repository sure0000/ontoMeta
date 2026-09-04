import { ToolOutlined } from "@ant-design/icons";
import { McpToolsPanel } from "../../components/McpPanel";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";

export function ToolsPage() {
  return (
    <PageContainer>
      <PageHeader
        icon={<ToolOutlined />}
        title="MCP 工具"
        description="查看 MCP 工具的职责和最低角色要求。"
      />
      <McpToolsPanel />
    </PageContainer>
  );
}
