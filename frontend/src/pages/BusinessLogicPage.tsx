import { FunctionOutlined, PlusOutlined, ImportOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import type { BusinessLogic, DomainContext } from "../types";

const { Text } = Typography;

const SOURCE_TYPE_OPTIONS = [
  { label: "SQL", value: "sql" },
  { label: "Python", value: "python" },
  { label: "其它", value: "other" },
];

const STATUS_FILTER_OPTIONS = [
  { label: "全部", value: "all" },
  { label: "草稿(suggested/edited/pre_published)", value: "draft" },
  { label: "已发布", value: "published" },
];

const DRAFT_STATUSES = new Set(["suggested", "edited", "pre_published"]);

export function BusinessLogicPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const domainId = searchParams.get("domain") || undefined;
  const statusFilter = searchParams.get("status") || "all";
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [logics, setLogics] = useState<BusinessLogic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [importOpen, setImportOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [importForm] = Form.useForm();

  useEffect(() => {
    api.listDomains().then(setDomains).catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (domains.length === 0) {
      setLoading(false);
      return;
    }

    const targetDomainId = domainId ?? domains[0]?.id;
    if (!targetDomainId) {
      setLoading(false);
      return;
    }

    if (!domainId && targetDomainId) {
      setSearchParams(
        { domain: targetDomainId, status: statusFilter },
        { replace: true },
      );
      return;
    }

    setLoading(true);
    api
      .listBusinessLogics({ domainId: targetDomainId })
      .then((list) => setLogics(list))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [domainId, domains, setSearchParams, statusFilter]);

  const domainsWithPublished = domains.filter((d) => d.published_count > 0);
  const targetDomainId = domainId ?? domains[0]?.id;
  const targetDomainHasPublished = domains.find((d) => d.id === targetDomainId)?.published_count ?? 0;

  const filteredLogics = logics.filter((l) => {
    if (statusFilter === "draft") return DRAFT_STATUSES.has(l.status);
    if (statusFilter === "published") return l.status === "published";
    return true;
  });

  const openCreate = () => {
    navigate(`/business-logic/create?domain=${targetDomainId ?? ""}`);
  };

  const openImport = () => {
    importForm.resetFields();
    importForm.setFieldsValue({
      domain_id: targetDomainHasPublished ? targetDomainId : domainsWithPublished[0]?.id,
      source_type: "sql",
    });
    setImportOpen(true);
  };

  const handleImport = async () => {
    const values = await importForm.validateFields();
    setSubmitting(true);
    try {
      const created = await api.importBusinessLogic(values);
      setImportOpen(false);
      message.success("已从代码导入业务逻辑草稿");
      navigate(`/business-logic/${created.id}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "导入失败");
    } finally {
      setSubmitting(false);
    }
  };

  const columns: ColumnsType<BusinessLogic> = [
    {
      title: "逻辑名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (_, record) => (
        <Link to={`/business-logic/${record.id}`} className="id-link">
          <span>{record.display_name}</span>
          <span className="id-link-sub">{record.name}</span>
        </Link>
      ),
    },
    {
      title: "类型",
      dataIndex: "logic_type",
      key: "logic_type",
      width: 130,
    },
    {
      title: "数据域",
      dataIndex: "domain_name",
      key: "domain_name",
      width: 160,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "来源",
      dataIndex: "source_type",
      key: "source_type",
      width: 130,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "引用对象",
      dataIndex: "bound_object_count",
      key: "bound_object_count",
      width: 100,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
    },
    {
      title: "引用字段",
      dataIndex: "bound_property_count",
      key: "bound_property_count",
      width: 100,
      align: "right",
      render: (v) => v ?? <span className="om-muted">-</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "置信度",
      dataIndex: "source_confidence",
      key: "source_confidence",
      width: 100,
      align: "right",
      render: (value?: number) =>
        value?.toFixed(2) ?? <span className="om-muted">-</span>,
    },
  ];

  if (loading && domains.length === 0) return <PageSkeleton type="list" />;

  const noPublishedHint =
    domainsWithPublished.length === 0
      ? "当前没有任何已发布本体,请先在工作区完成本体建模并发布,再创建业务逻辑。"
      : undefined;

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title="业务逻辑"
        description="独立于工作区的指标、标签与规则管理。引用已发布本体的对象与字段作为计算逻辑;支持代码导入与人工编辑。"
        extra={
          <Space wrap>
            <Text type="secondary" style={{ fontSize: 13 }}>
              数据域
            </Text>
            <Select
              style={{ minWidth: 220 }}
              value={targetDomainId}
              onChange={(value) =>
                setSearchParams({ domain: value, status: statusFilter }, { replace: true })
              }
              options={domains.map((d) => ({ label: d.name, value: d.id }))}
            />
            <Select
              style={{ minWidth: 180 }}
              value={statusFilter}
              onChange={(value) =>
                setSearchParams(
                  { domain: targetDomainId ?? "", status: value },
                  { replace: true },
                )
              }
              options={STATUS_FILTER_OPTIONS}
            />
            <Button icon={<ImportOutlined />} onClick={openImport} disabled={domainsWithPublished.length === 0}>
              导入业务逻辑
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={domainsWithPublished.length === 0}>
              新建业务逻辑
            </Button>
          </Space>
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

      {noPublishedHint && (
        <Alert
          type="info"
          message="尚无已发布本体"
          description={noPublishedHint}
          showIcon
          style={{ marginTop: 12 }}
        />
      )}

      <Spin spinning={loading}>
        {filteredLogics.length === 0 ? (
          <EmptyState
            title={logics.length === 0 ? "暂无业务逻辑" : "当前筛选下无业务逻辑"}
            description="业务逻辑独立于工作区管理。可选择「导入业务逻辑」从代码解析草稿,或「新建业务逻辑」手动创建。"
            action={
              <Link to="/workspace">
                <span className="om-link">前往工作区 →</span>
              </Link>
            }
          />
        ) : (
          <div className="section-card">
            <div className="section-card-head">
              <div className="section-card-head-title">
                <span>业务逻辑列表</span>
                <span className="section-card-count section-card-count--primary">
                  {filteredLogics.length}
                </span>
              </div>
            </div>
            <div className="section-card-body section-card-body--flush">
              <Table
                className="om-table"
                rowKey="id"
                size="middle"
                columns={columns}
                dataSource={filteredLogics}
                pagination={false}
              />
            </div>
          </div>
        )}
      </Spin>

      <Modal
        title="导入业务逻辑"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={handleImport}
        okText="解析并创建草稿"
        cancelText="取消"
        confirmLoading={submitting}
        destroyOnClose
        width={640}
      >
        <Form form={importForm} layout="vertical">
          <Form.Item
            label="所属数据域"
            name="domain_id"
            rules={[{ required: true, message: "请选择数据域" }]}
            extra="解析后的逻辑将归属该域的已发布本体,引用对象在详情页挑选"
          >
            <Select
              options={domainsWithPublished.map((d) => ({ label: d.name, value: d.id }))}
              placeholder="选择已发布本体的数据域"
            />
          </Form.Item>
          <Form.Item label="代码类型" name="source_type" rules={[{ required: true }]}>
            <Select options={SOURCE_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item label="代码" name="code" rules={[{ required: true, message: "请粘贴代码" }]}>
            <Input.TextArea
              rows={12}
              placeholder="粘贴 SQL / Python / 其它代码,LLM(或 Mock 规则)将解析为业务逻辑草稿"
              style={{ fontFamily: "monospace" }}
            />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
