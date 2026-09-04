import { KeyOutlined } from "@ant-design/icons";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";
import { PrincipalsPanel } from "../../components/PrincipalsPanel";

export function TokensPage() {
  return (
    <PageContainer>
      <PageHeader
        icon={<KeyOutlined />}
        title="令牌"
        description="为外部 Agent 创建最小权限 Principal 令牌，并追踪其调用。"
      />
      <PrincipalsPanel />
    </PageContainer>
  );
}
