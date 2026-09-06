export type MarkdownBlock =
  | { type: "code"; lang: string; code: string }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "hr" }
  | { type: "line"; raw: string };

function parseTableRow(line: string): string[] {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const text = line.trim();
  return text.includes("-") && /^\|?[\s:|-]+\|?$/.test(text);
}

function isThematicBreak(line: string): boolean {
  const text = line.trim();
  return /^(?:-{3,}|\*{3,}|_{3,})[\s-*_]*$/.test(text) && !text.includes("|");
}

export function splitMarkdownBlocks(content: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = content.split("\n");
  let index = 0;
  const push = (block: MarkdownBlock) => {
    const previous = blocks[blocks.length - 1];
    if (block.type === "hr" && previous?.type === "hr") return;
    if (
      block.type === "line" &&
      !block.raw.trim() &&
      previous &&
      ((previous.type === "line" && !previous.raw.trim()) || previous.type === "hr")
    ) {
      return;
    }
    blocks.push(block);
  };

  while (index < lines.length) {
    const line = lines[index];
    const fenceMatch = line.trim().match(/^```(\w*)$/);
    if (fenceMatch) {
      const lang = fenceMatch[1].toLowerCase();
      const codeLines: string[] = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index++;
      }
      index++;
      if (lang !== "sql") push({ type: "code", lang, code: codeLines.join("\n") });
      continue;
    }
    if (isThematicBreak(line)) {
      push({ type: "hr" });
      index++;
      continue;
    }
    if (
      line.trim().startsWith("|") &&
      index + 1 < lines.length &&
      isTableSeparator(lines[index + 1])
    ) {
      const header = parseTableRow(line);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(parseTableRow(lines[index]));
        index++;
      }
      push({ type: "table", header, rows });
      continue;
    }
    push({ type: "line", raw: line });
    index++;
  }

  while (blocks[0]?.type === "line" && !blocks[0].raw.trim()) blocks.shift();
  while (blocks.at(-1)?.type === "line" && !(blocks.at(-1) as { raw: string }).raw.trim()) {
    blocks.pop();
  }
  return blocks;
}

export function splitInlineTokens(
  text: string,
): Array<{ type: "text" | "bold" | "code" | "link"; value: string; href?: string }> {
  const parts: Array<{ type: "text" | "bold" | "code" | "link"; value: string; href?: string }> = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push({ type: "text", value: text.slice(lastIndex, match.index) });
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push({ type: "bold", value: token.slice(2, -2) });
    } else if (token.startsWith("`")) {
      parts.push({ type: "code", value: token.slice(1, -1) });
    } else {
      const link = /^\[([^\]]+)\]\(([^\s)]+)\)$/.exec(token);
      parts.push(link ? { type: "link", value: link[1], href: link[2] } : { type: "text", value: token });
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) parts.push({ type: "text", value: text.slice(lastIndex) });
  return parts;
}
