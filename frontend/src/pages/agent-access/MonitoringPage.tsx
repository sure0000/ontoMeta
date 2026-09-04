import { AuditOutlined } from "@ant-design/icons";
import { McpMonitoringPanel } from "../../components/McpPanel";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";

export function MonitoringPage() {
  return (
    <PageContainer>
      <PageHeader
        icon={<AuditOutlined />}
        title="审计监控"
        description="查看 MCP 调用统计、失败、拒绝和限流记录。"
      />
      <McpMonitoringPanel />
    </PageContainer>
  );
}
