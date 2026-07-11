import { DeleteOutlined, EditOutlined, FunctionOutlined, PlusOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Row,
  Spin,
  Typography,
  message,
} from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import type { BusinessLogicCategory } from "../types";

const { Paragraph } = Typography;

export function BusinessLogicPage() {
  const navigate = useNavigate();
  const [categories, setCategories] = useState<BusinessLogicCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [editingCategory, setEditingCategory] = useState<BusinessLogicCategory | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const load = () => {
    setLoading(true);
    api
      .listBusinessLogicCategories()
      .then(setCategories)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const openCreate = () => {
    setEditingCategory(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (cat: BusinessLogicCategory) => {
    setEditingCategory(cat);
    form.setFieldsValue({ name: cat.name, description: cat.description ?? "" });
    setModalOpen(true);
  };

  const handleSave = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editingCategory) {
        await api.updateBusinessLogicCategory(editingCategory.id, {
          name: values.name,
          description: values.description || undefined,
        });
        message.success("分类已更新");
      } else {
        await api.createBusinessLogicCategory({
          name: values.name,
          description: values.description || undefined,
        });
        message.success("分类已创建");
      }
      setModalOpen(false);
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteBusinessLogicCategory(id);
      message.success("分类已删除");
      load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  if (loading && categories.length === 0) return <PageSkeleton type="list" full />;

  return (
    <PageContainer full>
      <PageHeader
        icon={<FunctionOutlined />}
        title="业务逻辑管理"
        description="创建分类来组织管理业务逻辑，点击分类卡片进入查看"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建分类
          </Button>
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
          style={{ marginBottom: 16 }}
        />
      )}

      <Spin spinning={loading}>
        {categories.length === 0 ? (
          <Empty
            description="暂无分类"
            style={{ marginTop: 80 }}
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              创建第一个分类
            </Button>
          </Empty>
        ) : (
          <Row gutter={[16, 16]}>
            {categories.map((cat) => (
              <Col key={cat.id} xs={24} sm={12} md={8} lg={6}>
                <Card
                  hoverable
                  className="om-category-card"
                  onClick={() => navigate(`/business-logic/category/${cat.id}`)}
                  actions={[
                    <EditOutlined
                      key="edit"
                      onClick={(e) => {
                        e.stopPropagation();
                        openEdit(cat);
                      }}
                    />,
                    <Popconfirm
                      key="delete"
                      title={`确认删除分类「${cat.name}」？分类下的业务逻辑将移出该分类。`}
                      onConfirm={(e) => {
                        e?.stopPropagation();
                        handleDelete(cat.id);
                      }}
                      onCancel={(e) => e?.stopPropagation()}
                    >
                      <DeleteOutlined
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Popconfirm>,
                  ]}
                >
                  <Card.Meta
                    title={
                      <span>
                        {cat.name}
                        <span
                          style={{
                            marginLeft: 8,
                            color: "var(--om-text-secondary, #94a3b8)",
                            fontSize: 13,
                            fontWeight: 400,
                          }}
                        >
                          ({cat.logic_count})
                        </span>
                      </span>
                    }
                    description={
                      <Paragraph
                        ellipsis={{ rows: 2 }}
                        style={{ marginBottom: 0, color: "var(--om-text-secondary, #94a3b8)" }}
                      >
                        {cat.description || "暂无描述"}
                      </Paragraph>
                    }
                  />
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Spin>

      <Modal
        title={editingCategory ? "编辑分类" : "新建分类"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSave}
        okText={editingCategory ? "保存" : "创建"}
        cancelText="取消"
        confirmLoading={saving}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            label="分类名称"
            name="name"
            rules={[{ required: true, message: "请输入分类名称" }]}
          >
            <Input placeholder="例如：用户指标、风控规则" />
          </Form.Item>
          <Form.Item label="描述" name="description">
            <Input.TextArea rows={3} placeholder="可选，分类描述说明" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
