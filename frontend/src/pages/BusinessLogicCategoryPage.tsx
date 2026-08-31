import {
  EditOutlined,
  FunctionOutlined,
  PlusOutlined,
  ImportOutlined,
  SearchOutlined,
  SwapOutlined,
} from "@ant-design/icons";
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
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { StatusBadge } from "../components/StatusBadge";
import { UNCATEGORIZED_BUSINESS_LOGIC_CATEGORY_ID } from "../constants/businessLogic";
import { useApi } from "../hooks/useApi";
import type { BusinessLogic, BusinessLogicCategory, DomainContext } from "../types";

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

export function BusinessLogicCategoryPage() {
  const { categoryId } = useParams<{ categoryId: string }>();
  const navigate = useNavigate();

  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [importOpen, setImportOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [movingLogicId, setMovingLogicId] = useState<string | null>(null);
  const [moveLogic, setMoveLogic] = useState<BusinessLogic | null>(null);
  const [moveTargetCategoryId, setMoveTargetCategoryId] = useState<string>();
  const [importForm] = Form.useForm();

  const { data: categories, loading: categoriesLoading } = useApi<BusinessLogicCategory[]>(
    async () => api.listBusinessLogicCategories(),
    [],
  );

  const isUncategorized = categoryId === UNCATEGORIZED_BUSINESS_LOGIC_CATEGORY_ID;
  const category = (categories ?? []).find((c) => c.id === categoryId);

  const { data: domains, loading: domainsLoading } = useApi<DomainContext[]>(
    async () => api.listDomains(),
    [],
  );

  const {
    data: logics,
    loading,
    error,
    reload,
  } = useApi<BusinessLogic[]>(async () => {
    if (!categoryId) return [];
    const page = isUncategorized
      ? await api.listBusinessLogics({ uncategorized: true })
      : await api.listBusinessLogics({ categoryId });
    return page.items;
  }, [categoryId, isUncategorized]);

  const domainsWithPublished = (domains ?? []).filter((d) => d.published_count > 0);
  // 请求尚未完成时允许进入创建页，创建页会继续加载并选择数据域；请求完成后，
  // 没有已发布本体才禁用，避免按钮因慢请求长时间处于不可点击状态。
  const createDisabled = !domainsLoading && domainsWithPublished.length === 0;
  const moveTargets = [
    { key: UNCATEGORIZED_BUSINESS_LOGIC_CATEGORY_ID, label: "未分类" },
    ...(categories ?? []).map((item) => ({ key: item.id, label: item.name })),
  ];

  const filteredLogics = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (logics ?? []).filter((l) => {
      if (statusFilter === "draft" && !DRAFT_STATUSES.has(l.status)) return false;
      if (statusFilter === "published" && l.status !== "published") return false;
      if (!q) return true;
      if (l.name?.toLowerCase().includes(q)) return true;
      if (l.display_name?.toLowerCase().includes(q)) return true;
      if (l.description?.toLowerCase().includes(q)) return true;
      if (l.logic_type?.toLowerCase().includes(q)) return true;
      return false;
    });
  }, [logics, statusFilter, query]);

  const openCreate = () => {
    const targetDomainId = domainsWithPublished[0]?.id ?? "";
    const params = new URLSearchParams();
    if (targetDomainId) params.set("domain", targetDomainId);
    if (!isUncategorized && categoryId) params.set("category", categoryId);
    const query = params.toString();
    navigate(`/business-logic/create${query ? `?${query}` : ""}`);
  };

  const openImport = () => {
    importForm.resetFields();
    importForm.setFieldsValue({
      domain_id: domainsWithPublished[0]?.id,
      source_type: "sql",
    });
    setImportOpen(true);
  };

  const handleImport = async () => {
    const values = await importForm.validateFields();
    setSubmitting(true);
    try {
      const created = await api.importBusinessLogic({
        ...values,
        category_id: isUncategorized ? null : categoryId,
      });
      setImportOpen(false);
      message.success("已从代码导入业务逻辑草稿");
      navigate(`/business-logic/${created.id}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "导入失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleMove = async (logicId: string, targetCategoryId: string) => {
    const categoryIdForUpdate =
      targetCategoryId === UNCATEGORIZED_BUSINESS_LOGIC_CATEGORY_ID ? "" : targetCategoryId;
    setMovingLogicId(logicId);
    try {
      await api.updateBusinessLogic(logicId, { category_id: categoryIdForUpdate });
      message.success(categoryIdForUpdate ? "业务逻辑已迁移" : "业务逻辑已移至未分类");
      setMoveLogic(null);
      setMoveTargetCategoryId(undefined);
      await reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "迁移失败");
    } finally {
      setMovingLogicId(null);
    }
  };

  const openMove = (record: BusinessLogic) => {
    setMoveLogic(record);
    setMoveTargetCategoryId(undefined);
  };

  const columns: ColumnsType<BusinessLogic> = [
    {
      title: "逻辑名称",
      dataIndex: "display_name",
      key: "display_name",
      width: 240,
      ellipsis: true,
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
      width: 100,
    },
    {
      title: "数据域",
      dataIndex: "domain_name",
      key: "domain_name",
      width: 140,
      render: (v) => v || <span className="om-muted">-</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status) => <StatusBadge status={status} />,
    },
    {
      title: "操作",
      key: "action",
      width: 180,
      render: (_, record) => {
        return (
          <Space size={4}>
            <Button
              type="link"
              size="small"
              icon={<EditOutlined />}
              onClick={() => navigate(`/business-logic/${record.id}?edit=true`)}
            >
              编辑
            </Button>
            <Button
              type="link"
              size="small"
              icon={<SwapOutlined />}
              disabled={categoriesLoading || movingLogicId === record.id}
              onClick={() => openMove(record)}
            >
              迁移
            </Button>
          </Space>
        );
      },
    },
  ];

  if (loading && !category && !isUncategorized) return <PageSkeleton type="list" full />;

  if (!category && !isUncategorized) {
    return (
      <PageContainer>
        <Alert type="error" message="分类不存在" showIcon />
      </PageContainer>
    );
  }

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title={isUncategorized ? "未分类" : category?.name}
        description={
          isUncategorized ? "尚未归类的业务逻辑" : category?.description || undefined
        }
        extra={
          <Space wrap>
            <Input
              allowClear
              prefix={<SearchOutlined style={{ color: "var(--om-text-secondary)" }} />}
              placeholder="搜索逻辑名称、类型、描述"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ width: 220 }}
            />
            <Select
              style={{ minWidth: 180 }}
              value={statusFilter}
              onChange={setStatusFilter}
              options={STATUS_FILTER_OPTIONS}
            />
            <Button
              icon={<ImportOutlined />}
              onClick={openImport}
              disabled={domainsLoading || domainsWithPublished.length === 0}
            >
              导入业务逻辑
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={openCreate}
              disabled={createDisabled}
            >
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
          style={{ marginBottom: 16 }}
        />
      )}

      <Spin spinning={loading}>
        {(logics ?? []).length === 0 ? (
          <EmptyState
            title="暂无业务逻辑"
            description="在此分类下添加业务逻辑。可选择「导入业务逻辑」从代码解析草稿，或「新建业务逻辑」手动创建。"
            action={
              <Space>
                <Button
                  icon={<ImportOutlined />}
                  onClick={openImport}
                  disabled={domainsLoading || domainsWithPublished.length === 0}
                >
                  导入业务逻辑
                </Button>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={openCreate}
                  disabled={createDisabled}
                >
                  新建业务逻辑
                </Button>
              </Space>
            }
          />
        ) : filteredLogics.length === 0 ? (
          <EmptyState title="未匹配到业务逻辑" description="尝试调整搜索关键词或状态筛选。" />
        ) : (
          <Table
            className="om-table"
            rowKey="id"
            size="middle"
            columns={columns}
            dataSource={filteredLogics}
            pagination={{
              pageSize: 20,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50, 100],
              showTotal: (total) => `共 ${total} 条`,
            }}
          />
        )}
      </Spin>

      <Modal
        title={`迁移业务逻辑「${moveLogic?.display_name ?? ""}」`}
        open={moveLogic !== null}
        onCancel={() => {
          if (!movingLogicId) {
            setMoveLogic(null);
            setMoveTargetCategoryId(undefined);
          }
        }}
        maskClosable={!movingLogicId}
        closable={!movingLogicId}
        onOk={() => {
          if (moveLogic && moveTargetCategoryId) {
            void handleMove(moveLogic.id, moveTargetCategoryId);
          }
        }}
        okText="确认迁移"
        cancelText="取消"
        confirmLoading={movingLogicId !== null}
        okButtonProps={{ disabled: !moveTargetCategoryId }}
        destroyOnClose
      >
        <Form layout="vertical">
          <Form.Item label="目标分类" required>
            <Select
              autoFocus
              placeholder="请选择目标分类"
              value={moveTargetCategoryId}
              options={moveTargets
                .filter(
                  (item) =>
                    item.key !==
                    (moveLogic?.category_id || UNCATEGORIZED_BUSINESS_LOGIC_CATEGORY_ID),
                )
                .map((item) => ({ label: item.label, value: item.key }))}
              loading={categoriesLoading}
              disabled={categoriesLoading || movingLogicId !== null}
              onChange={setMoveTargetCategoryId}
            />
          </Form.Item>
        </Form>
      </Modal>

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
            extra="解析后的逻辑将归属该域的已发布本体"
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
              placeholder="粘贴 SQL / Python / 其它代码，LLM 将解析为业务逻辑草稿"
              style={{ fontFamily: "monospace" }}
            />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
