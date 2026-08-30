import { useState } from "react";
import { message } from "antd";
import "./CodeBlock.css";

export interface CodeBlockProps {
  /** 代码内容 */
  code: string;
  /** 语言标签（SQL / Python / JavaScript 等） */
  language?: string;
  /** 是否显示行号 */
  showLineNumbers?: boolean;
  /** 额外 CSS 类 */
  className?: string;
}

/**
 * 统一代码块组件：深色主题 + 头部栏（语言标签 + 复制按钮）+ 语法高亮。
 *
 * 用于：
 * - 正文围栏代码块（\`\`\`）
 * - 结构化 SQL 块
 * - 其他需要展示代码的场景
 */
export function CodeBlock({ code, language = "code", className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      message.success("已复制");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      message.error("复制失败");
    }
  };

  return (
    <div className={`code-block ${className || ""}`.trim()}>
      <div className="code-block-header">
        <span className="code-block-lang">{language.toUpperCase()}</span>
        <button
          type="button"
          className="code-block-copy"
          onClick={handleCopy}
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="code-block-pre">
        <code className="code-block-code">{code}</code>
      </pre>
    </div>
  );
}

/**
 * 简单的 SQL 语法高亮（手写分词器，轻量且流式友好）。
 *
 * 复用自原有的 SqlBlock，支持关键字/字符串/数字/注释高亮。
 */
export function highlightSql(sql: string): React.ReactNode[] {
  const lines = sql.split("\n");
  return lines.map((line, idx) => (
    <div key={idx} className="code-line">
      {tokenizeSqlLine(line).map((token, i) => (
        <span key={i} className={token.className}>
          {token.text}
        </span>
      ))}
    </div>
  ));
}

interface Token {
  text: string;
  className: string;
}

function tokenizeSqlLine(line: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;

  while (i < line.length) {
    // 注释
    if (line.slice(i, i + 2) === "--") {
      tokens.push({ text: line.slice(i), className: "code-comment" });
      break;
    }

    // 字符串
    if (line[i] === "'" || line[i] === '"') {
      const quote = line[i];
      let j = i + 1;
      while (j < line.length && line[j] !== quote) {
        if (line[j] === "\\") j++;
        j++;
      }
      tokens.push({ text: line.slice(i, j + 1), className: "code-string" });
      i = j + 1;
      continue;
    }

    // 数字
    if (/\d/.test(line[i])) {
      let j = i;
      while (j < line.length && /[\d.]/.test(line[j])) j++;
      tokens.push({ text: line.slice(i, j), className: "code-number" });
      i = j;
      continue;
    }

    // 标识符或关键字
    if (/[a-zA-Z_]/.test(line[i])) {
      let j = i;
      while (j < line.length && /[a-zA-Z0-9_]/.test(line[j])) j++;
      const word = line.slice(i, j);
      const isKeyword = SQL_KEYWORDS.has(word.toUpperCase());
      tokens.push({
        text: word,
        className: isKeyword ? "code-keyword" : "",
      });
      i = j;
      continue;
    }

    // 操作符和标点
    if (/[(),.;=<>!+\-*/]/.test(line[i])) {
      tokens.push({ text: line[i], className: "code-operator" });
      i++;
      continue;
    }

    // 空白符
    if (/\s/.test(line[i])) {
      let j = i;
      while (j < line.length && /\s/.test(line[j])) j++;
      tokens.push({ text: line.slice(i, j), className: "" });
      i = j;
      continue;
    }

    // 其他字符
    tokens.push({ text: line[i], className: "" });
    i++;
  }

  return tokens;
}

const SQL_KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
  "ON", "AS", "AND", "OR", "NOT", "IN", "LIKE", "BETWEEN", "IS", "NULL",
  "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC", "LIMIT", "OFFSET",
  "UNION", "INTERSECT", "EXCEPT", "WITH", "CASE", "WHEN", "THEN", "ELSE", "END",
  "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TABLE", "VIEW",
  "INDEX", "DATABASE", "SCHEMA", "PRIMARY", "KEY", "FOREIGN", "REFERENCES",
  "CONSTRAINT", "UNIQUE", "CHECK", "DEFAULT", "AUTO_INCREMENT",
  "SUM", "COUNT", "AVG", "MIN", "MAX", "DISTINCT", "CAST", "COALESCE",
]);
