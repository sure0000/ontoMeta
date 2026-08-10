import { useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { MinusCircleOutlined, PlusOutlined } from "@ant-design/icons";

import { api } from "../api";
import type { ObjectTypeSummary } from "../types";

const DIALECTS = [
  { label: "MySQL", value: "mysql" },
  { label: "PostgreSQL", value: "postgresql" },
  { label: "Hive", value: "hive" },
  { label: "SQLite", value: "sqlite" },
];

const SEMANTIC_TYPES = [
  { label: "标识 identifier", value: "identifier" },
  { label: "金额 amount", value: "amount" },
  { label: "时间 datetime", value: "datetime" },
  { label: "分类 category", value: "category" },
  { label: "标志 flag", value: "flag" },
  { label: "属性 attribute", value: "attribute" },
  { label: "文本 text", value: "text" },
];

interface PropertyRow {
  name: string;
  display_name?: string;
  semantic_type?: string;
  data_type?: string;
  primary_key?: boolean;
}

interface ObjectForm {
  name: string;
  display_name: string;
  description?: string;
  dialect: string;
  data_source?: string;
  properties: PropertyRow[];
}

interface RelationForm {
  source_object_type_id: string;
  target_object_type_id: string;
  display_name: string;
  cardinality?: string;
  structure_type?: string;
  description?: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  domainId: string;
  ontologyId?: string | null;
  objects: ObjectTypeSummary[];
  onCreated: () => void;
}

/**
 * 人工生成：本体先行地手工新增业务对象 / 业务关系。
 *
 * 与「从 DataHub 预生成」互补——预生成是对**历史/存量数据**做本体治理；人工生成
 * 面向**新业务**，先定义本体，再据此在选定数据源上创建物理表（生成建表 DDL）。
 */
export function ManualCreateModal({
  open,
  onClose,
  domainId,
  ontologyId,
  objects,
  onCreated,
}: Props) {
  const [mode, setMode] = useState<"object" | "relation">("object");
  const [objForm] = Form.useForm<ObjectForm>();
  const [relForm] = Form.useForm<RelationForm>();
  const [submitting, setSubmitting] = useState(false);
  const [ddl, setDdl] = useState<string | null>(null);

  const objectOptions = objects.map((o) => ({
    label: `${o.display_name}（${o.name}）`,
    value: o.id,
  }));

  const submitObject = async () => {
    const values = await objForm.validateFields();
    setSubmitting(true);
    try {
      const res = await api.createManualObject(domainId, {
        name: values.name,
        display_name: values.display_name,
        description: values.description,
        dialect: values.dialect,
        data_source: values.data_source,
        properties: (values.properties || []).filter((p) => p && p.name),
      });
      setDdl(res.ddl);
      message.success(`业务对象「${values.display_name}」已创建`);
      objForm.resetFields();
      onCreated();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const submitRelation = async () => {
    if (!ontologyId) {
      message.warning("请先创建业务对象后再新增关系");
      return;
    }
    const values = await relForm.validateFields();
    setSubmitting(true);
    try {
      await api.createRelationType({
        ontology_id: ontologyId,
        display_name: values.display_name,
        source_object_type_id: values.source_object_type_id,
        target_object_type_id: values.target_object_type_id,
        cardinality: values.cardinality,
        structure_type: values.structure_type,
        description: values.description,
      });
      message.success("业务关系已创建");
      relForm.resetFields();
      onCreated();
      onClose();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleClose = () => {
    setDdl(null);
    onClose();
  };

  return (
    <Modal
      title="人工生成"
      open={open}
      onCancel={handleClose}
      width={720}
      okText={mode === "object" ? "创建对象" : "创建关系"}
      confirmLoading={submitting}
      onOk={mode === "object" ? submitObject : submitRelation}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary" style={{ marginTop: -4 }}>
        本体先行：先手工定义业务对象与关系，再据此在选定数据源上创建物理表。适用于新业务从零
        以本体方式管理数据（区别于对历史数据做治理的「从 DataHub 预生成」）。
      </Typography.Paragraph>

      <Segmented
        block
        value={mode}
        onChange={(v) => {
          setMode(v as "object" | "relation");
          setDdl(null);
        }}
        options={[
          { label: "新增业务对象", value: "object" },
          { label: "新增业务关系", value: "relation" },
        ]}
        style={{ marginBottom: 16 }}
      />

      {mode === "object" ? (
        <Form
          form={objForm}
          layout="vertical"
          initialValues={{ dialect: "mysql", properties: [{}] }}
        >
          <Space size={12} style={{ display: "flex" }}>
            <Form.Item
              name="display_name"
              label="业务名称"
              rules={[{ required: true, message: "请输入业务名称" }]}
              style={{ flex: 1 }}
            >
              <Input placeholder="如：会员" />
            </Form.Item>
            <Form.Item
              name="name"
              label="标识名（英文）"
              rules={[{ required: true, message: "请输入英文标识名" }]}
              style={{ flex: 1 }}
            >
              <Input placeholder="如：loyalty_member" />
            </Form.Item>
          </Space>

          <Form.Item name="description" label="业务描述">
            <Input.TextArea rows={2} placeholder="一句话说明该业务对象" />
          </Form.Item>

          <Space size={12} style={{ display: "flex" }}>
            <Form.Item name="dialect" label="数据源方言" style={{ width: 200 }}>
              <Select options={DIALECTS} />
            </Form.Item>
            <Form.Item name="data_source" label="数据源标识（可选）" style={{ flex: 1 }}>
              <Input placeholder="如：mysql-new-biz（用于溯源标注）" />
            </Form.Item>
          </Space>

          <Typography.Text strong>属性（物理列）</Typography.Text>
          <Form.List name="properties">
            {(fields, { add, remove }) => (
              <div style={{ marginTop: 8 }}>
                {fields.map((field) => (
                  <Space
                    key={field.key}
                    align="baseline"
                    style={{ display: "flex", marginBottom: 4 }}
                  >
                    <Form.Item
                      {...field}
                      name={[field.name, "name"]}
                      rules={[{ required: true, message: "列名" }]}
                    >
                      <Input placeholder="列名 如 member_id" style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item {...field} name={[field.name, "display_name"]}>
                      <Input placeholder="中文名" style={{ width: 120 }} />
                    </Form.Item>
                    <Form.Item {...field} name={[field.name, "semantic_type"]}>
                      <Select
                        placeholder="语义类型"
                        options={SEMANTIC_TYPES}
                        style={{ width: 150 }}
                      />
                    </Form.Item>
                    <Form.Item
                      {...field}
                      name={[field.name, "primary_key"]}
                      valuePropName="checked"
                    >
                      <Checkbox>主键</Checkbox>
                    </Form.Item>
                    <MinusCircleOutlined onClick={() => remove(field.name)} />
                  </Space>
                ))}
                <Button type="dashed" onClick={() => add({})} icon={<PlusOutlined />} block>
                  添加属性
                </Button>
              </div>
            )}
          </Form.List>

          {ddl && (
            <Alert
              type="success"
              showIcon
              style={{ marginTop: 16 }}
              message="已生成建表 DDL（可在选定数据源执行）"
              description={
                <Typography.Paragraph
                  copyable={{ text: ddl }}
                  style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", marginBottom: 0 }}
                >
                  {ddl}
                </Typography.Paragraph>
              }
            />
          )}
        </Form>
      ) : (
        <Form form={relForm} layout="vertical">
          {objects.length < 1 || !ontologyId ? (
            <Alert type="info" showIcon message="请先创建至少一个业务对象后再新增业务关系。" />
          ) : (
            <>
              <Space size={12} style={{ display: "flex" }}>
                <Form.Item
                  name="source_object_type_id"
                  label="源对象"
                  rules={[{ required: true, message: "请选择源对象" }]}
                  style={{ flex: 1 }}
                >
                  <Select
                    showSearch
                    optionFilterProp="label"
                    options={objectOptions}
                    placeholder="源对象"
                  />
                </Form.Item>
                <Form.Item
                  name="display_name"
                  label="关系谓词"
                  rules={[{ required: true, message: "如：属于 / 引用 / 下单" }]}
                  style={{ width: 160 }}
                >
                  <Input placeholder="如：属于" />
                </Form.Item>
                <Form.Item
                  name="target_object_type_id"
                  label="目标对象"
                  rules={[{ required: true, message: "请选择目标对象" }]}
                  style={{ flex: 1 }}
                >
                  <Select
                    showSearch
                    optionFilterProp="label"
                    options={objectOptions}
                    placeholder="目标对象"
                  />
                </Form.Item>
              </Space>
              <Space size={12} style={{ display: "flex" }}>
                <Form.Item name="cardinality" label="基数" style={{ width: 160 }}>
                  <Select
                    allowClear
                    placeholder="基数"
                    options={[
                      { label: "N:1", value: "N:1" },
                      { label: "1:N", value: "1:N" },
                      { label: "1:1", value: "1:1" },
                      { label: "N:M", value: "N:M" },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="structure_type" label="结构类型" style={{ width: 180 }}>
                  <Select
                    allowClear
                    placeholder="结构类型"
                    options={[
                      { label: "外键关系", value: "foreign_key" },
                      { label: "桥表", value: "bridge_table" },
                      { label: "事实表", value: "fact_table" },
                      { label: "派生/溯源", value: "derivation" },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="description" label="说明" style={{ flex: 1 }}>
                  <Input placeholder="可选：业务语义说明" />
                </Form.Item>
              </Space>
            </>
          )}
        </Form>
      )}
    </Modal>
  );
}
