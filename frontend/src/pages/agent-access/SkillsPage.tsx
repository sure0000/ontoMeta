import {
  BookOutlined,
  CheckOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  DiffOutlined,
  EyeOutlined,
  HistoryOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import { Alert, Button, Col, Divider, Input, List, Modal, Row, Space, Switch, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";
import { SectionCard } from "../../components/SectionCard";
import type { McpSkill, McpSkillVersion, McpToolInfo } from "../../types";
import { MarkdownLite } from "../chat-bi/ChatBiReferences";

const { Text, Paragraph } = Typography;

export function SkillsPage() {
  const [skills, setSkills] = useState<McpSkill[]>([]);
  const [tools, setTools] = useState<McpToolInfo[]>([]);
  const [selected, setSelected] = useState<string>();
  const [detail, setDetail] = useState<McpSkill | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(false);
  const [showBuiltin, setShowBuiltin] = useState(false);
  const [editing, setEditing] = useState(false);
  const [versions, setVersions] = useState<McpSkillVersion[]>([]);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionPreview, setVersionPreview] = useState<McpSkillVersion | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [result, info] = await Promise.all([api.getMcpSkills(), api.getMcpInfo()]);
      setSkills(result.skills);
      setTools(info.tools);
      setSelected((current) => current ?? result.skills[0]?.name);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载 Skill 失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (name: string) => {
    try {
      const [value, history] = await Promise.all([
        api.getMcpSkill(name),
        api.listMcpSkillVersions(name),
      ]);
      setDetail(value);
      setDraft(value.body);
      setShowBuiltin(false);
      setEditing(false);
      setVersions(history.versions);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载 Skill 详情失败");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (selected) void loadDetail(selected); }, [selected, loadDetail]);

  const coveredTools = useMemo(
    () => new Set(skills.filter((item) => item.enabled).flatMap((item) => item.mentioned_tools)),
    [skills],
  );
  const save = async () => {
    if (!selected || !editing) return;
    setSaving(true);
    try {
      await api.updateMcpSkill(selected, draft);
      message.success("Skill 覆写已保存");
      await load();
      await loadDetail(selected);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally { setSaving(false); }
  };

  const reset = async () => {
    if (!selected || !editing) return;
    setSaving(true);
    try {
      await api.resetMcpSkill(selected);
      message.success("已恢复内置版本");
      await load();
      await loadDetail(selected);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "恢复失败");
    } finally { setSaving(false); }
  };

  const restoreVersion = (version: McpSkillVersion) => {
    if (!selected) return;
    Modal.confirm({
      title: `回滚到 v${version.version}？`,
      content: "回滚不会删除现有版本，而是创建一个新的版本记录。",
      okText: "确认回滚",
      cancelText: "取消",
      onOk: async () => {
        setSaving(true);
        try {
          await api.restoreMcpSkillVersion(selected, version.version);
          message.success(`已回滚到 v${version.version}`);
          setVersionsOpen(false);
          await load();
          await loadDetail(selected);
        } catch (err) {
          message.error(err instanceof Error ? err.message : "回滚失败");
        } finally {
          setSaving(false);
        }
      },
    });
  };

  const toggle = async (name: string, enabled: boolean) => {
    try {
      await api.setMcpSkillEnabled(name, enabled);
      await load();
      if (name === selected) await loadDetail(name);
    } catch (err) { message.error(err instanceof Error ? err.message : "更新启用状态失败"); }
  };

  const exportSkills = async (name?: string) => {
    try {
      await api.downloadMcpSkills(name);
      message.success(name ? `已导出 ${name}` : "已导出全部已启用 Skill");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "导出失败");
    }
  };

  return (
    <PageContainer>
      <PageHeader
        icon={<BookOutlined />}
        title="技能"
      />
      <SectionCard
        title="导出 Skill"
        icon={<DownloadOutlined />}
        extra={<Button type="primary" icon={<DownloadOutlined />} onClick={() => void exportSkills()}>导出全部已启用</Button>}
      >
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Text>导出当前生效版本，ZIP 内按 <Text code>&lt;skill-name&gt;/SKILL.md</Text> 组织，可直接复制到支持 Skill 的客户端目录。</Text>
          {selected && <Button icon={<DownloadOutlined />} onClick={() => void exportSkills(selected)}>导出当前 Skill：{selected}</Button>}
        </Space>
      </SectionCard>
      <SectionCard title="工具覆盖度" icon={<CheckOutlined />}>
        <Alert
          type={tools.length > 0 && tools.every((tool) => coveredTools.has(tool.name)) ? "success" : "warning"}
          showIcon
          message={`${tools.filter((tool) => coveredTools.has(tool.name)).length} / ${tools.length} 个工具已有 Skill 指引`}
          description="保存覆写时服务端会检查全部注册工具仍被至少一份启用 Skill 覆盖。"
        />
        <Space wrap style={{ marginTop: 12 }}>
          {tools.map((tool) => <Tag key={tool.name} color={coveredTools.has(tool.name) ? "green" : "red"}>{tool.name}</Tag>)}
        </Space>
      </SectionCard>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={9} lg={7}>
          <SectionCard title="Skill 清单" count={skills.length} extra={<Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} />}>
            <List
              dataSource={skills}
              renderItem={(item) => (
                <List.Item
                  onClick={() => setSelected(item.name)}
                  style={{ cursor: "pointer", paddingInline: 8, background: item.name === selected ? "var(--om-bg-soft)" : undefined }}
                  actions={[<Switch key="enabled" size="small" checked={item.enabled} onChange={(value) => void toggle(item.name, value)} />]}
                >
                  <List.Item.Meta
                    title={<Text strong>{item.name}</Text>}
                    description={<Space size={4} wrap><Tag>{item.source === "override" ? "已改写" : "builtin"}</Tag><Text type="secondary">{item.tool_count} 工具</Text>{item.upstream_updated && <Tag color="orange">上游已更新</Tag>}</Space>}
                  />
                </List.Item>
              )}
            />
          </SectionCard>
        </Col>
        <Col xs={24} md={15} lg={17}>
          <SectionCard
            title={selected ?? "选择 Skill"}
            extra={<Space wrap><Button icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>版本历史{versions.length ? ` (${versions.length})` : ""}</Button>{!editing ? <Button icon={<EditOutlined />} onClick={() => setEditing(true)} disabled={!detail}>编辑</Button> : <><Button onClick={() => { setDraft(detail?.body ?? ""); setEditing(false); }}>取消</Button><Button icon={<DeleteOutlined />} onClick={() => void reset()} disabled={!detail?.override} loading={saving}>恢复默认</Button><Button type="primary" icon={<SaveOutlined />} onClick={() => void save()} disabled={!selected} loading={saving}>保存新版本</Button></>}{<Button icon={<EyeOutlined />} onClick={() => setPreview((value) => !value)}>{preview ? "编辑视图" : "预览"}</Button>}{detail?.upstream_updated && <Button icon={<DiffOutlined />} onClick={() => setShowBuiltin((value) => !value)}>{showBuiltin ? "隐藏上游" : "查看上游 diff"}</Button>}</Space>}
          >
            {detail && <>
              <Paragraph type="secondary">{String(detail.frontmatter.whenToUse ?? detail.frontmatter.description ?? "")}</Paragraph>
              <Divider />
              {preview ? <div className="chatbi-md" style={{ maxHeight: 620, overflow: "auto" }}><MarkdownLite content={draft} /></div> : <Input.TextArea value={draft} readOnly={!editing} onChange={(event) => setDraft(event.target.value)} autoSize={{ minRows: 24, maxRows: 42 }} />}
              {showBuiltin && <><Divider /><Text strong>内置版本</Text><pre style={{ whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto", marginTop: 8 }}>{detail.builtin_body}</pre></>}
            </>}
          </SectionCard>
        </Col>
      </Row>
      <Modal
        title={`${selected ?? "Skill"} 版本历史`}
        open={versionsOpen}
        onCancel={() => setVersionsOpen(false)}
        footer={null}
        width={760}
      >
        <List
          locale={{ emptyText: "还没有编辑版本，内置默认版本不可修改。点击“编辑”后保存会创建第一个版本。" }}
          dataSource={versions}
          renderItem={(version) => (
            <List.Item
              actions={[
                <Button key="view" size="small" icon={<EyeOutlined />} onClick={() => setVersionPreview(version)}>查看</Button>,
                <Button key="restore" size="small" icon={<RollbackOutlined />} onClick={() => restoreVersion(version)}>回滚</Button>,
              ]}
            >
              <List.Item.Meta
                title={<Space><Text strong>v{version.version}</Text><Tag color={version.action === "restore" ? "gold" : "blue"}>{version.action === "restore" ? "恢复默认" : "编辑保存"}</Tag></Space>}
                description={`${version.created_at ? new Date(version.created_at).toLocaleString() : "未知时间"}${version.created_by ? ` · ${version.created_by}` : ""}`}
              />
            </List.Item>
          )}
        />
      </Modal>
      <Modal
        title={versionPreview ? `${selected ?? "Skill"} v${versionPreview.version}` : "版本内容"}
        open={Boolean(versionPreview)}
        onCancel={() => setVersionPreview(null)}
        footer={null}
        width={820}
      >
        {versionPreview && <pre style={{ whiteSpace: "pre-wrap", maxHeight: "65vh", overflow: "auto", margin: 0 }}>{versionPreview.body}</pre>}
      </Modal>
    </PageContainer>
  );
}
