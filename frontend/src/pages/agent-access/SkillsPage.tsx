import {
  BookOutlined,
  CheckOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FolderOpenOutlined,
  DiffOutlined,
  EyeOutlined,
  HistoryOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Col,
  Divider,
  Input,
  List,
  Modal,
  Row,
  Skeleton,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { PageContainer } from "../../components/PageContainer";
import { PageHeader } from "../../components/PageHeader";
import { SectionCard } from "../../components/SectionCard";
import type {
  McpSkill,
  McpSkillInstallResult,
  McpSkillVersion,
  McpToolInfo,
} from "../../types";
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
  const [showComposed, setShowComposed] = useState(false);
  const [installDir, setInstallDir] = useState("");
  const [installing, setInstalling] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [result, info] = await Promise.all([api.getMcpSkills(), api.getMcpInfo()]);
      setSkills(result.skills);
      setTools(info.tools);
      setSelected((current) => current ?? result.skills[0]?.name);
      // 安装目录只是"上次装到哪"，取不到不该让整页报错——那会把 Skill 清单一起吞掉。
      try {
        const settings = await api.getMcpSettings();
        setInstallDir((current) => current || settings.mcp_skill_install_dir || "");
      } catch {
        // 读不到设置就让用户自己填一次。
      }
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
      // 编辑的是**原文**（带 {{OUTPUT_CONTRACT}} 占位符）。拿合成正文去编辑，
      // 一保存就把契约固化进这份 skill，从此改总控不再影响它。
      setDraft(value.source_body);
      setShowBuiltin(false);
      setShowComposed(false);
      setEditing(false);
      setVersions(history.versions);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载 Skill 详情失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (selected) void loadDetail(selected);
  }, [selected, loadDetail]);

  const coveredTools = useMemo(
    () => new Set(skills.filter((item) => item.enabled).flatMap((item) => item.mentioned_tools)),
    [skills],
  );
  const uncoveredTools = useMemo(
    () => tools.map((tool) => tool.name).filter((name) => !coveredTools.has(name)),
    [tools, coveredTools],
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
    } finally {
      setSaving(false);
    }
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
    } finally {
      setSaving(false);
    }
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
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新启用状态失败");
    }
  };

  /**
   * 装到目录 = 服务端直接把生效正文写成 `<目录>/<skill-name>/SKILL.md`。
   *
   * 先 dry_run 拿计划给人看再写：这是往**后端主机**上写文件的动作，目标目录里往往还放着
   * 别的 Agent 技能，"会新建哪几份、会覆盖哪几份"必须在按下确认之前就看得见。
   */
  const installSkills = async () => {
    const dir = installDir.trim();
    if (!dir) {
      message.warning("先填 Agent 读取 Skill 的目录（后端主机上的绝对路径）");
      return;
    }
    setInstalling(true);
    let plan: McpSkillInstallResult;
    try {
      plan = await api.installMcpSkills({ target_dir: dir, dry_run: true });
    } catch (err) {
      message.error(err instanceof Error ? err.message : "安装预检失败");
      return;
    } finally {
      setInstalling(false);
    }
    const changed = plan.items.filter((item) => item.action !== "unchanged");
    Modal.confirm({
      title: "安装 Skill 到目录",
      width: 640,
      okText: changed.length ? `写入 ${plan.items.length} 份` : "重新写入一遍",
      cancelText: "取消",
      content: (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Text>
            目标目录：<Text code>{plan.target_dir}</Text>
            {!plan.exists && <Text type="secondary">（不存在，将自动创建）</Text>}
          </Text>
          <Text type="secondary">
            新建 {plan.created} · 覆盖 {plan.updated} · 已是最新 {plan.unchanged}；
            只写 <Text code>&lt;skill-name&gt;/SKILL.md</Text>，目录里其它文件不动。
          </Text>
          <div style={{ maxHeight: 240, overflow: "auto" }}>
            <List
              size="small"
              dataSource={plan.items}
              renderItem={(item) => (
                <List.Item>
                  <Space size={6}>
                    <Tag
                      color={
                        item.action === "created"
                          ? "green"
                          : item.action === "updated"
                            ? "orange"
                            : undefined
                      }
                    >
                      {item.action === "created"
                        ? "新建"
                        : item.action === "updated"
                          ? "覆盖"
                          : "未变"}
                    </Tag>
                    <Text>{item.name}</Text>
                  </Space>
                </List.Item>
              )}
            />
          </div>
        </Space>
      ),
      onOk: async () => {
        try {
          const result = await api.installMcpSkills({ target_dir: dir });
          setInstallDir(result.target_dir);
          message.success(`已写入 ${result.written?.length ?? 0} 份到 ${result.target_dir}`);
        } catch (err) {
          message.error(err instanceof Error ? err.message : "安装失败");
          throw err; // 让对话框留在原地，用户可以改路径重试
        }
      },
    });
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
      <PageHeader icon={<BookOutlined />} title="技能" />
      <SectionCard title="部署 Skill" icon={<FolderOpenOutlined />}>
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Text>
            把当前生效版本按 <Text code>&lt;skill-name&gt;/SKILL.md</Text>{" "}
            直接写进 Agent 读取 Skill 的目录，不必再下载解压。
          </Text>
          <Space.Compact style={{ width: "100%" }}>
            <Input
              value={installDir}
              onChange={(event) => setInstallDir(event.target.value)}
              onPressEnter={() => void installSkills()}
              placeholder="后端主机上的绝对路径，如 /Users/you/.dsh/skills"
              prefix={<FolderOpenOutlined />}
              allowClear
            />
            <Button
              type="primary"
              onClick={() => void installSkills()}
              loading={installing}
              disabled={!installDir.trim()}
            >
              安装到目录
            </Button>
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 12 }}>
            路径是 <Text strong>ontoMeta 后端所在主机</Text>
            上的目录（安装由服务端写盘，不是浏览器下载）；Agent 装在别的机器上时用下面的 ZIP。
            dsh 的目录见 <Text code>skill-filesystem.customSkillDirs</Text>。会先给出预检计划再写。
          </Text>
          <Divider style={{ margin: "4px 0" }} />
          <Space wrap>
            <Button icon={<DownloadOutlined />} onClick={() => void exportSkills()}>
              下载全部已启用（ZIP）
            </Button>
            {selected && (
              <Button icon={<DownloadOutlined />} onClick={() => void exportSkills(selected)}>
                下载当前 Skill：{selected}
              </Button>
            )}
          </Space>
        </Space>
      </SectionCard>
      <SectionCard title="工具覆盖度" icon={<CheckOutlined />}>
        {/* 有信息量的是「哪几个还没覆盖」。全覆盖时把 30 个绿色 chip 全铺出来，
            读者要逐个扫一遍才能确认"确实没有红的"——那正是它想省掉的事。 */}
        {loading ? (
          <Skeleton active paragraph={{ rows: 1 }} title={false} />
        ) : (
          <>
            <Alert
              type={uncoveredTools.length === 0 ? "success" : "warning"}
              showIcon
              message={
                uncoveredTools.length === 0
                  ? `全部 ${tools.length} 个工具都有 Skill 指引`
                  : `${uncoveredTools.length} 个工具还没有 Skill 指引（共 ${tools.length} 个）`
              }
              description="保存覆写时服务端会检查全部注册工具仍被至少一份启用 Skill 覆盖。"
            />
            {uncoveredTools.length > 0 && (
              <Space wrap style={{ marginTop: 12 }}>
                {uncoveredTools.map((name) => (
                  <Tag key={name} color="red">
                    {name}
                  </Tag>
                ))}
              </Space>
            )}
          </>
        )}
      </SectionCard>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={9} lg={7}>
          <SectionCard
            title="Skill 清单"
            count={loading && skills.length === 0 ? undefined : skills.length}
            extra={
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => void load()}
                loading={loading}
              />
            }
          >
            {/* 首屏加载期显示「Skill 清单 0 / 暂无数据」会被读成"真的一个都没有"。 */}
            {loading && skills.length === 0 ? (
              <Skeleton active paragraph={{ rows: 6 }} title={false} />
            ) : null}
            <List
              style={loading && skills.length === 0 ? { display: "none" } : undefined}
              dataSource={skills}
              renderItem={(item) => (
                <List.Item
                  onClick={() => setSelected(item.name)}
                  style={{
                    cursor: "pointer",
                    paddingInline: 8,
                    background: item.name === selected ? "var(--om-bg-soft)" : undefined,
                  }}
                  actions={[
                    <Switch
                      key="enabled"
                      size="small"
                      checked={item.enabled}
                      // 停用总控 = 所有回答同时失去格式约束，而界面上只表现为"某份 skill 灰了"。
                      disabled={item.is_output_contract}
                      onChange={(value) => void toggle(item.name, value)}
                    />,
                  ]}
                >
                  <List.Item.Meta
                    title={<Text strong>{item.name}</Text>}
                    description={
                      <Space size={4} wrap>
                        <Tag>{item.source === "override" ? "已改写" : "builtin"}</Tag>
                        {item.is_output_contract ? (
                          <Tag color="purple">出口契约总控</Tag>
                        ) : (
                          <Text type="secondary">{item.tool_count} 工具</Text>
                        )}
                        {/* 契约被本地固化 = 这份不再跟随总控更新。不标出来的话，
                            改了总控却有一份没变，只能靠逐字比对才发现。 */}
                        {item.contract_source === "inline" && <Tag color="gold">契约本地固化</Tag>}
                        {item.upstream_updated && <Tag color="orange">上游已更新</Tag>}
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          </SectionCard>
        </Col>
        <Col xs={24} md={15} lg={17}>
          <SectionCard
            title={selected ?? "选择 Skill"}
            extra={
              <Space wrap>
                <Button icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
                  版本历史{versions.length ? ` (${versions.length})` : ""}
                </Button>
                {!editing ? (
                  <Button
                    icon={<EditOutlined />}
                    onClick={() => setEditing(true)}
                    disabled={!detail}
                  >
                    编辑
                  </Button>
                ) : (
                  <>
                    <Button
                      onClick={() => {
                        setDraft(detail?.source_body ?? "");
                        setEditing(false);
                      }}
                    >
                      取消
                    </Button>
                    <Button
                      icon={<DeleteOutlined />}
                      onClick={() => void reset()}
                      disabled={!detail?.override}
                      loading={saving}
                    >
                      恢复默认
                    </Button>
                    <Button
                      type="primary"
                      icon={<SaveOutlined />}
                      onClick={() => void save()}
                      disabled={!selected}
                      loading={saving}
                    >
                      保存新版本
                    </Button>
                  </>
                )}
                {
                  <Button icon={<EyeOutlined />} onClick={() => setPreview((value) => !value)}>
                    {preview ? "编辑视图" : "预览"}
                  </Button>
                }
                {!detail?.is_output_contract && (
                  <Button icon={<BookOutlined />} onClick={() => setShowComposed((value) => !value)}>
                    {showComposed ? "隐藏下发正文" : "查看下发正文"}
                  </Button>
                )}
                {detail?.upstream_updated && (
                  <Button icon={<DiffOutlined />} onClick={() => setShowBuiltin((value) => !value)}>
                    {showBuiltin ? "隐藏上游" : "查看上游 diff"}
                  </Button>
                )}
              </Space>
            }
          >
            {detail && (
              <>
                {/* description 是中文、whenToUse 是给模型看的英文触发语。
                  这里原本只显示 whenToUse——整页中文里插一段英文，而中文那句反倒没露面。 */}
                <Paragraph style={{ marginBottom: 4 }}>
                  {String(detail.frontmatter.description ?? "")}
                </Paragraph>
                {detail.frontmatter.whenToUse ? (
                  <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                    <Text type="secondary" strong style={{ fontSize: 12 }}>
                      模型触发语：
                    </Text>
                    {String(detail.frontmatter.whenToUse)}
                  </Paragraph>
                ) : null}
                {detail.is_output_contract ? (
                  <Alert
                    style={{ marginTop: 8 }}
                    type="info"
                    showIcon
                    message="这份是出口契约总控"
                    description="正文里「输出格式（必须遵守）」以下的全部内容，会替换掉其它 Skill 里的 {{OUTPUT_CONTRACT}} 占位符。改这里等于同时改掉所有 Skill 的回答格式、状态口径与提问方式。"
                  />
                ) : detail.contract_source === "inherited" ? (
                  <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
                    出口契约由 <Text code>ontometa-output</Text> 提供（正文里的{" "}
                    <Text code>{"{{OUTPUT_CONTRACT}}"}</Text> 会在下发、导出和安装时替换成它的正文）。
                  </Paragraph>
                ) : detail.contract_source === "inline" ? (
                  <Alert
                    style={{ marginTop: 8 }}
                    type="warning"
                    showIcon
                    message="这份自带了一份输出契约，不跟随总控更新"
                    description={`改回跟随：把正文里的输出契约段落换成 {{OUTPUT_CONTRACT}} 占位符再保存。`}
                  />
                ) : null}
                <Divider />
                {preview ? (
                  <div className="chatbi-md" style={{ maxHeight: 620, overflow: "auto" }}>
                    <MarkdownLite content={draft} />
                  </div>
                ) : (
                  <Input.TextArea
                    value={draft}
                    readOnly={!editing}
                    onChange={(event) => setDraft(event.target.value)}
                    autoSize={{ minRows: 24, maxRows: 42 }}
                  />
                )}
                {showComposed && (
                  <>
                    <Divider />
                    <Text strong>下发正文（Agent 实际拿到的，含注入的出口契约）</Text>
                    <pre
                      style={{
                        whiteSpace: "pre-wrap",
                        maxHeight: 360,
                        overflow: "auto",
                        marginTop: 8,
                      }}
                    >
                      {detail.body}
                    </pre>
                  </>
                )}
                {showBuiltin && (
                  <>
                    <Divider />
                    <Text strong>内置版本</Text>
                    <pre
                      style={{
                        whiteSpace: "pre-wrap",
                        maxHeight: 360,
                        overflow: "auto",
                        marginTop: 8,
                      }}
                    >
                      {detail.builtin_body}
                    </pre>
                  </>
                )}
              </>
            )}
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
          locale={{
            emptyText: "还没有编辑版本，内置默认版本不可修改。点击“编辑”后保存会创建第一个版本。",
          }}
          dataSource={versions}
          renderItem={(version) => (
            <List.Item
              actions={[
                <Button
                  key="view"
                  size="small"
                  icon={<EyeOutlined />}
                  onClick={() => setVersionPreview(version)}
                >
                  查看
                </Button>,
                <Button
                  key="restore"
                  size="small"
                  icon={<RollbackOutlined />}
                  onClick={() => restoreVersion(version)}
                >
                  回滚
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={
                  <Space>
                    <Text strong>v{version.version}</Text>
                    <Tag color={version.action === "restore" ? "gold" : "blue"}>
                      {version.action === "restore" ? "恢复默认" : "编辑保存"}
                    </Tag>
                  </Space>
                }
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
        {versionPreview && (
          <pre style={{ whiteSpace: "pre-wrap", maxHeight: "65vh", overflow: "auto", margin: 0 }}>
            {versionPreview.body}
          </pre>
        )}
      </Modal>
    </PageContainer>
  );
}
