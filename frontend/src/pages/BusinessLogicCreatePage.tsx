import { FunctionOutlined } from "@ant-design/icons";
import { Alert, Button, Form, Input, Select, Spin, message } from "antd";
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import type { DomainContext } from "../types";

const LOGIC_TYPE_OPTIONS = [
  { label: "指标 metric", value: "metric" },
  { label: "标签 tag", value: "tag" },
  { label: "规则 rule", value: "rule" },
];

export function BusinessLogicCreatePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;

  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    api
      .listDomains()
      .then(setDomains)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const domainsWithPublished = domains.filter((d) => d.published_count > 0);

  if (loading) return <PageSkeleton type="detail" />;

  if (domainsWithPublished.length === 0) {
    return (
      <PageContainer full>
        <PageHeader
          icon={<FunctionOutlined />}
          title="新建业务逻辑"
          extra={
            <Button onClick={() => navigate("/business-logic")}>返回列表</Button>
          }
        />
        <Alert
          type="info"
          message="尚无已发布本体"
          description="当前没有任何已发布本体,请先在工作区完成本体建模并发布,再创建业务逻辑。"
          showIcon
        />
      </PageContainer>
    );
  }

  const handleCreate = async () => {
    const values = await form.validateFields();
    setSubmitting(true);
    try {
      const created = await api.createBusinessLogic(values);
      message.success("已创建业务逻辑草稿");
      navigate(`/business-logic/${created.id}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title="新建业务逻辑"
        description="创建指标、标签或规则草稿,引用已发布本体的对象与字段。"
        extra={
          <Button onClick={() => navigate("/business-logic")}>取消</Button>
        }
      />

      {error && (
        <Alert
          type="error"
          message="加载失败"
          description={error}
          showIcon
          closable
          onClose={() => setError(null)}
        />
      )}

      <Spin spinning={submitting}>
        <div className="section-card" style={{ marginTop: 16 }}>
          <div className="section-card-body">
            <Form
              form={form}
              layout="vertical"
              initialValues={{
                domain_id: domainId && domainsWithPublished.find((d) => d.id === domainId)
                  ? domainId
                  : domainsWithPublished[0]?.id,
                logic_type: "metric",
              }}
            >
              <Form.Item
                label="所属数据域"
                name="domain_id"
                rules={[{ required: true, message: "请选择数据域" }]}
                extra="业务逻辑将归属该域的已发布本体"
              >
                <Select
                  options={domainsWithPublished.map((d) => ({ label: d.name, value: d.id }))}
                  placeholder="选择已发布本体的数据域"
                />
              </Form.Item>
              <Form.Item
                label="标识名(英文)"
                name="name"
                rules={[{ required: true, message: "请输入标识名" }]}
              >
                <Input placeholder="如 order_gmv_metric" />
              </Form.Item>
              <Form.Item
                label="显示名"
                name="display_name"
                rules={[{ required: true, message: "请输入显示名" }]}
              >
                <Input placeholder="如 订单 GMV" />
              </Form.Item>
              <Form.Item label="逻辑类型" name="logic_type" rules={[{ required: true }]}>
                <Select options={LOGIC_TYPE_OPTIONS} />
              </Form.Item>
              <Form.Item label="描述" name="description">
                <Input.TextArea rows={2} placeholder="一句话说明业务含义" />
              </Form.Item>
              <Form.Item label="表达式摘要" name="expression_summary">
                <Input.TextArea rows={3} placeholder="如 SUM(amount) WHERE status='paid'" />
              </Form.Item>
              <Form.Item>
                <Button type="primary" onClick={handleCreate} loading={submitting}>
                  创建
                </Button>
              </Form.Item>
            </Form>
          </div>
        </div>
      </Spin>
    </PageContainer>
  );
}
