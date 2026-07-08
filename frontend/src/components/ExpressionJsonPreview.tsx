import { CodeOutlined, CopyOutlined } from "@ant-design/icons";
import { Button, Empty, Spin, Tooltip, message } from "antd";
import { useState } from "react";
import type { ExpressionJson } from "../types";

interface Props {
  json?: ExpressionJson | null;
  loading?: boolean;
  title?: string;
  emptyHint?: string;
  /** 嵌入 SectionCard 时使用，隐藏重复标题 */
  embedded?: boolean;
}

export function ExpressionJsonPreview({
  json,
  loading,
  title = "已格式化 JSON",
  emptyHint = "点击「预览」或「保存」后,LLM 将把表达式格式化为统一的 JSON",
  embedded = false,
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
    <div className={`expression-json-preview${embedded ? " expression-json-preview--embedded" : ""}`}>
      {!embedded && (
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
      )}
      <div className="expression-json-preview__body">
        {embedded && json && (
          <div className="expression-json-preview__toolbar">
            <Tooltip title={copied ? "已复制" : "复制 JSON"}>
              <Button
                size="small"
                type="text"
                icon={<CopyOutlined />}
                onClick={handleCopy}
              />
            </Tooltip>
          </div>
        )}
        {loading ? (
          <div className="expression-json-preview__loading">
            <Spin size="small" /> <span className="om-muted">LLM 格式化中…</span>
          </div>
        ) : json ? (
          <pre className="code-block code-block--light code-block--bounded">{JSON.stringify(json, null, 2)}</pre>
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
