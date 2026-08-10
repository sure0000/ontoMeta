import { useEffect, useState } from "react";
import { Button, Divider, Input, message, Modal, Space, Switch, Tag, Typography } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { PublicShareStatus } from "../types";

const { Text, Paragraph } = Typography;

export function ShareModal({
  open,
  appId,
  published,
  onClose,
}: {
  open: boolean;
  appId: string;
  published: boolean;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<PublicShareStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [password, setPassword] = useState("");
  const [expiresDays, setExpiresDays] = useState<number | undefined>();

  useEffect(() => {
    if (!open) return;
    api
      .getShareStatus(appId)
      .then(setStatus)
      .catch(() => setStatus(null));
  }, [open, appId]);

  const publicUrl = status?.public_token
    ? `${window.location.origin}/public/apps/${status.public_token}`
    : null;

  const handleToggle = async (checked: boolean) => {
    setLoading(true);
    try {
      const next = checked
        ? await api.enableShare(appId, {
            password: password || undefined,
            expires_in_days: expiresDays,
          })
        : await api.disableShare(appId);
      setStatus(next);
      message.success(checked ? "已开启公开分享" : "已关闭公开分享");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "操作失败");
    } finally {
      setLoading(false);
    }
  };

  const handleUpdate = async () => {
    setLoading(true);
    try {
      const next = await api.enableShare(appId, {
        password: password || undefined,
        expires_in_days: expiresDays,
      });
      setStatus(next);
      message.success("已更新分享设置");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "更新失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal title="公开分享" open={open} onCancel={onClose} footer={null} width={560}>
      {!published && <Paragraph type="warning">请先发布该应用，再开启公开分享。</Paragraph>}
      <Space align="center" style={{ marginBottom: 12 }}>
        <Switch
          checked={status?.public_enabled ?? false}
          disabled={!published || loading}
          onChange={handleToggle}
        />
        <Text>{status?.public_enabled ? "已开启（免登录只读访问）" : "未开启"}</Text>
      </Space>

      {status?.public_enabled && publicUrl && (
        <>
          <Space.Compact style={{ width: "100%", marginBottom: 8 }}>
            <Input readOnly value={publicUrl} />
            <Button
              icon={<CopyOutlined />}
              onClick={() => {
                void navigator.clipboard?.writeText(publicUrl);
                message.success("链接已复制");
              }}
            >
              复制
            </Button>
          </Space.Compact>
          <div style={{ marginBottom: 8 }}>
            {status.password_set && <Tag color="orange">已设口令</Tag>}
            {status.public_expires_at && (
              <Tag color="blue">
                有效期至 {new Date(status.public_expires_at).toLocaleDateString()}
              </Tag>
            )}
          </div>
        </>
      )}

      <Divider />
      <Text type="secondary" style={{ fontSize: 12 }}>
        访问控制（可选，修改后点击「更新设置」生效；开启分享时也会应用）
      </Text>
      <Space direction="vertical" style={{ width: "100%", marginTop: 8 }}>
        <Input.Password
          placeholder="访问口令（留空表示无口令）"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={!published}
        />
        <Space>
          <Text type="secondary">有效期</Text>
          <Input
            type="number"
            style={{ width: 160 }}
            placeholder="有效天数（留空长期）"
            value={expiresDays ?? ""}
            onChange={(e) => setExpiresDays(e.target.value ? Number(e.target.value) : undefined)}
            disabled={!published}
          />
          <Button
            onClick={handleUpdate}
            disabled={!published || !status?.public_enabled}
            loading={loading}
          >
            更新设置
          </Button>
        </Space>
      </Space>
    </Modal>
  );
}
