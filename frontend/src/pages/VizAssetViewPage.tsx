import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, Button, Space, Spin } from "antd";
import { ArrowLeftOutlined, ExportOutlined } from "@ant-design/icons";
import { embedDashboard } from "@superset-ui/embedded-sdk";
import { api } from "../api";
import { PageContainer } from "../components/PageContainer";
import { PageHeader } from "../components/PageHeader";
import type { SupersetAsset } from "../types";

/**
 * Superset 看板的只读内嵌预览。
 *
 * guest token **只能由后端签发**（`/api/superset/assets/{id}/guest-token`）：它是用
 * Superset 的服务账号换来的，凭据绝不能下发到浏览器。SDK 的 `fetchGuestToken` 每次
 * 都会重新调用，故这里不缓存 token——它本来就是短时效的。
 */
export function VizAssetViewPage() {
  const { assetId = "" } = useParams();
  const navigate = useNavigate();
  const mountRef = useRef<HTMLDivElement>(null);
  const [asset, setAsset] = useState<SupersetAsset | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchGuestToken = useCallback(async () => {
    const { token } = await api.supersetGuestToken(assetId);
    return token;
  }, [assetId]);

  useEffect(() => {
    let cancelled = false;
    let teardown: (() => void) | undefined;

    (async () => {
      try {
        const { items } = await api.listSupersetAssets();
        const found = items.find((a) => a.id === assetId) ?? null;
        if (cancelled) return;
        setAsset(found);
        if (!found) {
          setError("找不到这个资产，可能已被解除登记。");
          return;
        }
        if (!found.embedded_uuid) {
          setError(
            "这个看板还没开启嵌入。多半是 Superset 未启用 EMBEDDED_SUPERSET 特性开关；" +
              "看板本身可用，点右上角在 Superset 中打开。",
          );
          return;
        }
        const status = await api.getSupersetStatus();
        if (cancelled || !mountRef.current) return;
        if (!status.base_url) {
          setError(status.reason ?? "Superset 未配置");
          return;
        }
        const embedded = await embedDashboard({
          id: found.embedded_uuid,
          supersetDomain: status.base_url,
          mountPoint: mountRef.current,
          fetchGuestToken,
          dashboardUiConfig: { hideTitle: true, filters: { expanded: false } },
        });
        teardown = () => embedded.unmount();
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "加载看板失败");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      teardown?.();
    };
  }, [assetId, fetchGuestToken]);

  return (
    <PageContainer>
      <PageHeader
        title={asset?.title ?? "看板预览"}
        description="内容由 Superset 渲染；ontoMeta 只负责取嵌入令牌。"
        extra={
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate("/data-apps")}>
              返回
            </Button>
            {asset ? (
              <Button
                type="primary"
                icon={<ExportOutlined />}
                href={asset.url}
                target="_blank"
                rel="noreferrer"
              >
                在 Superset 中打开
              </Button>
            ) : null}
          </Space>
        }
      />

      {error ? (
        <Alert type="warning" showIcon message="无法内嵌预览" description={error} />
      ) : null}

      <Spin spinning={loading}>
        <div
          ref={mountRef}
          style={{
            minHeight: 640,
            border: "1px solid var(--om-border)",
            borderRadius: 8,
            overflow: "hidden",
            background: "var(--om-surface)",
          }}
        />
      </Spin>
    </PageContainer>
  );
}
