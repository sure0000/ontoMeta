import { splitInlineTokens, splitMarkdownBlocks } from "../utils/markdown";

/** Small dependency-free Markdown renderer shared by agent-facing panels. */
export function MarkdownLite({ content }: { content: string }) {
  const blocks = splitMarkdownBlocks(content);
  let key = 0;
  return (
    <div className="agent-markdown">
      {blocks.map((block) => {
        if (block.type === "code") {
          return (
            <pre key={key++} className="agent-markdown-codeblock">
              <code>{block.code}</code>
            </pre>
          );
        }
        if (block.type === "table") {
          return <MarkdownTable key={key++} header={block.header} rows={block.rows} />;
        }
        if (block.type === "hr") {
          return <hr key={key++} className="agent-markdown-hr" />;
        }
        return <Line key={key++} raw={block.raw} />;
      })}
    </div>
  );
}

function MarkdownTable({ header, rows }: { header: string[]; rows: string[][] }) {
  return (
    <div className="agent-markdown-tablewrap">
      <table className="agent-markdown-table">
        <thead>
          <tr>
            {header.map((cell, index) => (
              <th key={index}>
                <InlineRender text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex}>
                  <InlineRender text={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Line({ raw }: { raw: string }) {
  if (!raw.trim()) return <div className="agent-markdown-line" />;
  if (/^(?:-{3,}|\*{3,}|_{3,})[\s-*_]*$/.test(raw.trim()) && !raw.includes("|")) {
    return <hr className="agent-markdown-hr" />;
  }
  if (raw.trim().startsWith(">")) {
    return (
      <blockquote className="agent-markdown-quote">
        <InlineRender text={raw.replace(/^\s*>\s?/, "")} />
      </blockquote>
    );
  }
  const indent = /^\s+/.exec(raw)?.[0].replace(/\t/g, "  ").length ?? 0;
  const indentPx = Math.min(indent, 24) * 7;
  const orderedMatch = raw.match(/^\s*(\d+)\.\s+(.*)$/);
  if (orderedMatch) {
    return (
      <div
        className="agent-markdown-listitem agent-markdown-listitem--ordered"
        style={{ marginLeft: indentPx }}
      >
        <span className="agent-markdown-num">{orderedMatch[1]}</span>
        <span>
          <InlineRender text={orderedMatch[2]} />
        </span>
      </div>
    );
  }
  const listMatch = raw.match(/^\s*[-*]\s+(.*)$/);
  if (listMatch) {
    return (
      <div className="agent-markdown-listitem" style={{ marginLeft: indentPx }}>
        <span className="agent-markdown-bullet">•</span>
        <span>
          <InlineRender text={listMatch[1]} />
        </span>
      </div>
    );
  }
  const headerMatch = raw.match(/^(#{1,4})\s+(.*)$/);
  if (headerMatch) {
    const className = `agent-markdown-h${Math.min(headerMatch[1].length, 4)}`;
    return (
      <div className={className}>
        <InlineRender text={headerMatch[2]} />
      </div>
    );
  }
  return (
    <div className="agent-markdown-line">
      <InlineRender text={raw} />
    </div>
  );
}

function InlineRender({ text }: { text: string }) {
  const parts = splitInlineTokens(text);
  let key = 0;
  return (
    <>
      {parts.map((part) => {
        if (part.type === "bold") return <strong key={key++}>{part.value}</strong>;
        if (part.type === "code") {
          return (
            <code key={key++} className="agent-markdown-inline-code">
              {part.value}
            </code>
          );
        }
        if (part.type === "link") {
          return (
            <a
              key={key++}
              className="agent-markdown-link"
              href={part.href}
              target="_blank"
              rel="noopener noreferrer"
            >
              {part.value}
            </a>
          );
        }
        return <span key={key++}>{part.value}</span>;
      })}
    </>
  );
}
