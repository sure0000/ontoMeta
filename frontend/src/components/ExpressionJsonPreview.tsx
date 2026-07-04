import { CodeOutlined, CopyOutlined } from "@ant-design/icons";
import { Button, Empty, Spin, Tooltip, message } from "antd";
import { useState } from "react";
import type { ExpressionJson } from "../types";

interface Props {
  json?: ExpressionJson | null;
  loading?: boolean;
  title?: string;
  emptyHint?: string;
}

export function ExpressionJsonPreview({
  json,
  loading,
  title = "已格式化 JSON",
  emptyHint = "点击「预览」或「保存」后,LLM 将把表达式格式化为统一的 JSON",
}: Props) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!json) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(json, null, 2));
      setCopied(true);
      message.success("已复制");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      message.error("复制失败");
    }
  };

  return (
    <div className="expression-json-preview">
      <div className="expression-json-preview__head">
        <span className="expression-json-preview__title">
          <CodeOutlined /> {title}
        </span>
        {json && (
          <Tooltip title={copied ? "已复制" : "复制 JSON"}>
            <Button
              size="small"
              type="text"
              icon={<CopyOutlined />}
              onClick={handleCopy}
            />
          </Tooltip>
        )}
      </div>
      <div className="expression-json-preview__body">
        {loading ? (
          <div className="expression-json-preview__loading">
            <Spin size="small" /> <span className="om-muted">LLM 格式化中…</span>
          </div>
        ) : json ? (
          <pre className="code-block code-block--light">{JSON.stringify(json, null, 2)}</pre>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span className="om-muted">{emptyHint}</span>}
          />
        )}
      </div>
    </div>
  );
}
