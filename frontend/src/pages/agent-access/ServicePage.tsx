import { ApiOutlined } from "@ant-design/icons";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";
import { McpServicePanel } from "../../components/McpPanel";

export function ServicePage() {
  return (
    <PageContainer>
      <PageHeader
        icon={<ApiOutlined />}
        title="MCP 配置"
      />
      <McpServicePanel />
    </PageContainer>
  );
}
