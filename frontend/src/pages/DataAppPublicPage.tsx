import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Button, Card, Empty, Input, Result, Space, Spin, Typography } from "antd";
import {
  BarChartRender,
  DataTableRender,
  KpiRender,
} from "../components/DataAppRenderer";
import { DashboardGrid, type DashboardTile } from "../components/DashboardGrid";
import type { DataAppColumn } from "../types";

const { Title, Text } = Typography;

interface PreviewLike {
  dataset_id?: string;
  columns: DataAppColumn[];
  rows: Record<string, unknown>[];
}

interface PublicPayload {
  id: string;
  name: string;
  app_type: string;
  description?: string | null;
  spec?: Record<string, unknown> | null;
  datasets: { id: string; name: string }[];
  render: {
    datasets: PreviewLike[];
    widgets: Record<string, PreviewLike>;
  };
}

/** 免登录公开只读页。路径 /public/apps/:token */
export function DataAppPublicPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<PublicPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [needPassword, setNeedPassword] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (pwd?: string) => {
      if (!token) return;
      setLoading(true);
      setError(null);
      try {
        const qs = pwd ? `?password=${encodeURIComponent(pwd)}` : "";
        const resp = await fetch(`/api/public/data-apps/${token}${qs}`);
        if (resp.status === 401) {
          setNeedPassword(true);
          setLoading(false);
          return;
        }
        if (resp.status === 403) {
          setError("访问口令错误");
          setLoading(false);
          return;
        }
        if (!resp.ok) {
          setError("分享链接不存在或已关闭");
          setLoading(false);
          return;
        }
        const body = (await resp.json()) as PublicPayload;
        setData(body);
        setNeedPassword(false);
      } catch {
        setError("加载失败，请稍后重试");
      } finally {
        setLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div style={{ padding: 48, textAlign: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  if (needPassword) {
    return (
      <div style={{ maxWidth: 360, margin: "80px auto", padding: 24 }}>
        <Title level={4}>需要访问口令</Title>
        <Space.Compact style={{ width: "100%" }}>
          <Input.Password
            placeholder="请输入访问口令"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onPressEnter={() => void load(password)}
          />
          <Button type="primary" onClick={() => void load(password)}>
            访问
          </Button>
        </Space.Compact>
        {error && <Text type="danger" style={{ display: "block", marginTop: 8 }}>{error}</Text>}
      </div>
    );
  }

  if (error || !data) {
    return <Result status="404" title={error ?? "分享不可用"} />;
  }

  const isDashboard = data.app_type === "dashboard";
  const tiles = (data.spec?.tiles as DashboardTile[]) ?? [];
  const previewByIndex: Record<number, PreviewLike> = {};
  (data.render.datasets ?? []).forEach((p, i) => {
    previewByIndex[i] = p;
  });
  const widgetPreviews = data.render.widgets ?? {};

  const renderDataset = (i: number, type?: string) => {
    const p = previewByIndex[i];
    if (!p) return <Empty description="暂无数据" />;
    const props = { columns: p.columns, rows: p.rows };
    if (type === "bar") return <BarChartRender {...props} />;
    if (type === "kpi") return <KpiRender {...props} />;
    return <DataTableRender {...props} />;
  };

  return (
    <div style={{ padding: 16, minHeight: "100vh", background: "#f5f7fa" }}>
      <div style={{ maxWidth: 1280, margin: "0 auto" }}>
        <Title level={3} style={{ marginBottom: 4 }}>
          {data.name}
        </Title>
        {data.description && (
          <Text type="secondary" style={{ display: "block", marginBottom: 16 }}>
            {data.description}
          </Text>
        )}

        {isDashboard ? (
          <DashboardGrid
            tiles={tiles}
            grid={data.spec?.grid as { cols?: number; rowHeight?: number; gap?: number }}
            theme={data.spec?.theme as { bg?: string; accent?: string; preset?: string }}
            datasets={data.datasets}
            previews={previewByIndex as never}
            widgetPreviews={widgetPreviews as never}
          />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }} size="large">
            {data.datasets.map((ds, i) => (
              <Card key={ds.id} title={ds.name}>
                {renderDataset(i)}
              </Card>
            ))}
          </Space>
        )}

        <div style={{ textAlign: "center", marginTop: 24 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            由 ontoMeta Data Agent 提供
          </Text>
        </div>
      </div>
    </div>
  );
}
