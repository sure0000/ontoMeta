import { FunctionOutlined } from "@ant-design/icons";
import { Alert, Button, Col, Form, Input, Modal, Row, Select, Spin, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ExpressionJsonPreview } from "../components/ExpressionJsonPreview";
import { ExpressionRichEditor } from "../components/ExpressionRichEditor";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import type { DomainContext, ExpressionDraft, ExpressionJson } from "../types";

const LOGIC_TYPE_OPTIONS = [
  { label: "指标 metric", value: "metric" },
  { label: "标签 tag", value: "tag" },
  { label: "规则 rule", value: "rule" },
];

export function BusinessLogicCreatePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const categoryId = searchParams.get("category") || undefined;

  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewJson, setPreviewJson] = useState<ExpressionJson | null>(null);
  const [previewModalOpen, setPreviewModalOpen] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    api
      .listDomains()
      .then(setDomains)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const domainsWithPublished = domains.filter((d) => d.published_count > 0);

  const domainId = useMemo(() => {
    const urlDomainId = searchParams.get("domain");
    if (urlDomainId && domainsWithPublished.find((d) => d.id === urlDomainId)) {
      return urlDomainId;
    }
    return domainsWithPublished[0]?.id ?? "";
  }, [domainsWithPublished, searchParams]);

  if (loading) return <PageSkeleton type="detail" />;

  if (!domainId) {
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

  const formatDraft = async (
    draft: ExpressionDraft,
    logicType: string,
    description: string | undefined,
  ) => {
    return api.formatExpression({
      domain_id: domainId,
      expression_draft: draft,
      logic_type: logicType,
      description,
    });
  };

  const handlePreview = async () => {
    const values = await form.validateFields();
    const draft = values.expression_draft as ExpressionDraft | undefined;
    if (!draft || !draft.segments || draft.segments.length === 0) {
      message.warning("请先填写表达式");
      return;
    }
    setPreviewing(true);
    setPreviewModalOpen(true);
    try {
      const res = await formatDraft(
        draft,
        values.logic_type,
        values.description,
      );
      setPreviewJson(res.expression_json);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "预览失败");
      setPreviewModalOpen(false);
    } finally {
      setPreviewing(false);
    }
  };

  const handleCreate = async () => {
    const values = await form.validateFields();
    const draft = values.expression_draft as ExpressionDraft | undefined;
    if (!draft || !draft.segments || draft.segments.length === 0) {
      message.warning("请先填写表达式");
      return;
    }
    setSubmitting(true);
    try {
      const formatted = await formatDraft(
        draft,
        values.logic_type,
        values.description,
      );
      const created = await api.createBusinessLogic({
        domain_id: domainId,
        name: values.name,
        display_name: values.display_name,
        logic_type: values.logic_type,
        description: values.description,
        expression_draft: draft,
        expression_json: formatted.expression_json,
        expression_summary: formatted.expression_summary,
        category_id: categoryId ?? null,
      });
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
        description="用自然语言描述计算/规则,用 @ 引用已发布本体的对象与字段。保存时由 LLM 格式化为统一 JSON。"
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
              initialValues={{ logic_type: "metric" }}
            >
              <Row gutter={[16, 8]}>
                <Col xs={24} md={12}>
                  <Form.Item
                    label="逻辑类型"
                    name="logic_type"
                    rules={[{ required: true }]}
                  >
                    <Select options={LOGIC_TYPE_OPTIONS} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item
                    label="标识名(英文)"
                    name="name"
                    rules={[{ required: true, message: "请输入标识名" }]}
                  >
                    <Input placeholder="如 order_gmv_metric" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item
                    label="显示名"
                    name="display_name"
                    rules={[{ required: true, message: "请输入显示名" }]}
                  >
                    <Input placeholder="如 订单 GMV" />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item label="描述" name="description">
                    <Input.TextArea rows={2} placeholder="一句话说明业务含义" />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item
                label="表达式"
                name="expression_draft"
                extra={
                  <span style={{ fontSize: 12 }}>
                    用自然语言书写即可,输入 <code>@</code> 选择已发布本体的对象,选中后紧接
                    <code>.</code> 引用其字段;其余文字可任意描述
                  </span>
                }
              >
                <ExpressionRichEditor
                  domainId={domainId}
                  placeholder="例如:统计 SUM(@订单.金额) 万元,其中 @订单.状态 为「已支付」,按 @订单.城市 分组"
                  minHeight={200}
                />
              </Form.Item>

              <Form.Item style={{ marginBottom: 0 }}>
                <Button onClick={handlePreview} loading={previewing} style={{ marginRight: 8 }}>
                  JSON 预览
                </Button>
                <Button type="primary" onClick={handleCreate} loading={submitting}>
                  保存
                </Button>
              </Form.Item>
            </Form>
          </div>
        </div>
      </Spin>

      <Modal
        title="表达式 JSON 预览"
        open={previewModalOpen}
        onCancel={() => setPreviewModalOpen(false)}
        footer={[
          <Button key="close" onClick={() => setPreviewModalOpen(false)}>
            关闭
          </Button>,
        ]}
        width={720}
        destroyOnClose
      >
        <ExpressionJsonPreview
          json={previewJson}
          loading={previewing}
          title="AST 结构"
          emptyHint="LLM 正在将自然语言 + 引用格式化为统一的 AST JSON…"
        />
      </Modal>
    </PageContainer>
  );
}
