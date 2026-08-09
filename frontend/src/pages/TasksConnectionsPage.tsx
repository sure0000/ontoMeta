import { ApiOutlined } from "@ant-design/icons";
import { SyncRunnerSecretsPanel } from "../components/SyncRunnerSecretsPanel";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";

/**
 * 连接配置页
 *
 * 管理数据源连接信息，用于任务执行时连接源库和目标库。
 * 连接信息以别名形式存储在 sync-runner 中，确保凭据安全。
 */
export function TasksConnectionsPage() {
  return (
    <PageContainer>
      <PageHeader
        icon={<ApiOutlined />}
        title="连接配置"
        description="配置数据源连接信息，供同步、物化、加工等任务使用。连接凭据安全存储在 sync-runner 中。"
      />
      <SyncRunnerSecretsPanel />
    </PageContainer>
  );
}
