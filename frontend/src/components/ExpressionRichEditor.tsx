import { Spin } from "antd";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { offset, flip, shift, useFloating } from "@floating-ui/react";
import { api } from "../api";
import type {
  ExpressionDraft,
  ExpressionRefSegment,
  ExpressionSegment,
  ObjectTypeSummary,
  Property,
} from "../types";

interface Props {
  value?: ExpressionDraft;
  onChange?: (draft: ExpressionDraft) => void;
  domainId?: string;
  publishedOnly?: boolean;
  placeholder?: string;
  disabled?: boolean;
  minHeight?: number;
}

interface ObjectTrigger {
  kind: "object";
  node: Text;
  /** `@` 在文本节点中的偏移 */
  atOffset: number;
  /** 当前过滤文本的结束偏移(随光标移动更新) */
  endOffset: number;
  query: string;
}

interface PropertyTrigger {
  kind: "property";
  node: Text;
  /** `.` 在文本节点中的偏移 */
  atOffset: number;
  endOffset: number;
  query: string;
  /** 要扩展的 chip 元素 */
  chipEl: HTMLElement;
  /** chip 当前对应的对象 id */
  objectTypeId: string;
}

type Trigger = ObjectTrigger | PropertyTrigger;

const BLOCK_TAGS = new Set(["DIV", "P"]);

function genRefId(counter: number): string {
  return `r${counter}`;
}

function chipLabel(seg: ExpressionRefSegment): string {
  const obj = seg.object_display_name || seg.object_name || "?";
  if (seg.property_id) {
    const prop = seg.property_display_name || seg.property_name || "?";
    return `${obj}.${prop}`;
  }
  return obj;
}

function createChipEl(seg: ExpressionRefSegment, onRemove: () => void): HTMLElement {
  const span = document.createElement("span");
  span.className = "expr-chip" + (seg.property_id ? " expr-chip--property" : " expr-chip--object");
  span.setAttribute("contenteditable", "false");
  span.dataset.refId = seg.ref_id;
  span.dataset.objectTypeId = seg.object_type_id;
  span.dataset.objectName = seg.object_name;
  span.dataset.objectDisplayName = seg.object_display_name;
  if (seg.property_id) span.dataset.propertyId = seg.property_id;
  if (seg.property_name) span.dataset.propertyName = seg.property_name;
  if (seg.property_display_name) span.dataset.propertyDisplayName = seg.property_display_name;

  const label = document.createElement("span");
  label.className = "expr-chip__label";
  label.textContent = chipLabel(seg);
  span.appendChild(label);

  const del = document.createElement("button");
  del.type = "button";
  del.className = "expr-chip__remove";
  del.setAttribute("aria-label", "移除引用");
  del.textContent = "×";
  del.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    span.remove();
    onRemove();
  });
  span.appendChild(del);
  return span;
}

function segmentsToHtml(segments: ExpressionSegment[], onRemove: () => void): string {
  let html = "";
  for (const seg of segments) {
    if (seg.type === "text") {
      // 用 <br> 表示换行,其余走 textContent 转义
      const lines = seg.value.split("\n");
      for (let i = 0; i < lines.length; i++) {
        if (i > 0) html += "<br>";
        html += escapeHtml(lines[i]);
      }
    } else {
      const el = createChipEl(seg, onRemove);
      html += el.outerHTML;
    }
  }
  return html;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function domToSegments(root: HTMLElement): ExpressionSegment[] {
  const out: ExpressionSegment[] = [];
  let textBuf = "";
  const flush = () => {
    if (textBuf) {
      out.push({ type: "text", value: textBuf });
      textBuf = "";
    }
  };
  const walk = (node: Node, atLineStart: boolean): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      textBuf += node.textContent || "";
      return;
    }
    if (node.nodeName === "BR") {
      textBuf += "\n";
      return;
    }
    const el = node as HTMLElement;
    if (el.dataset && el.dataset.refId) {
      flush();
      out.push({
        type: "ref",
        ref_id: el.dataset.refId,
        object_type_id: el.dataset.objectTypeId || "",
        object_name: el.dataset.objectName || "",
        object_display_name: el.dataset.objectDisplayName || "",
        property_id: el.dataset.propertyId || undefined,
        property_name: el.dataset.propertyName || undefined,
        property_display_name: el.dataset.propertyDisplayName || undefined,
      });
      return;
    }
    const isBlock = BLOCK_TAGS.has(node.nodeName);
    if (isBlock && !atLineStart && out.length > 0) {
      textBuf += "\n";
    }
    for (const child of node.childNodes) {
      walk(child, isBlock || atLineStart);
    }
    if (isBlock) {
      textBuf += "\n";
    }
  };
  for (const child of root.childNodes) {
    walk(child, false);
  }
  flush();
  // 去掉末尾多余换行
  while (out.length > 0) {
    const last = out[out.length - 1];
    if (last.type !== "text") break;
    const trimmed = last.value.replace(/\n+$/, "");
    if (trimmed) {
      last.value = trimmed;
      break;
    }
    out.pop();
  }
  return out;
}

/** 在光标附近检测 `@对象` 或 `对象.` 触发上下文 */
function detectTrigger(): Trigger | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  let container: Node | null = range.startContainer;
  if (container.nodeType !== Node.TEXT_NODE) return null;
  const textNode = container as Text;
  const offset = range.startOffset;
  const text = textNode.data;
  if (offset === 0) return null;

  // 1) 对象模式:光标前最近一个 `@`(未被空白截断)
  let i = offset - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === "@") {
      const beforeOk =
        i === 0 || /\s/.test(text[i - 1]) || /[（(\[,.]/.test(text[i - 1]);
      if (beforeOk) {
        const query = text.slice(i + 1, offset);
        if (!/\s/.test(query)) {
          return {
            kind: "object",
            node: textNode,
            atOffset: i,
            endOffset: offset,
            query,
          };
        }
      }
      break;
    }
    if (/\s/.test(ch)) break;
    i -= 1;
  }

  // 2) 属性模式:光标前最近一个 `.`,且 `.` 前紧邻一个 chip
  i = offset - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === ".") {
      const query = text.slice(i + 1, offset);
      if (/\s/.test(query)) return null;
      // 检查 textNode 之前紧邻的兄弟节点是否为 chip
      const chipEl = findAdjacentChipBefore(textNode);
      if (chipEl && chipEl.dataset.objectTypeId) {
        return {
          kind: "property",
          node: textNode,
          atOffset: i,
          endOffset: offset,
          query,
          chipEl,
          objectTypeId: chipEl.dataset.objectTypeId,
        };
      }
      return null;
    }
    if (/\s/.test(ch)) break;
    i -= 1;
  }
  return null;
}

function findAdjacentChipBefore(textNode: Text): HTMLElement | null {
  let prev: Node | null = textNode.previousSibling;
  while (prev) {
    if (prev.nodeType === Node.ELEMENT_NODE) {
      const el = prev as HTMLElement;
      if (el.dataset && el.dataset.refId && !el.dataset.propertyId) {
        return el;
      }
      return null;
    }
    if ((prev.textContent || "").length > 0) return null;
    prev = prev.previousSibling;
  }
  return null;
}

/** 从当前 selection 获取光标视口坐标; WebKit 下 collapsed range height=0,用行高补全 */
function getCaretRect(): DOMRect | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  const br = range.getBoundingClientRect();
  if (br.height > 0) return br;
  if (br.width === 0 && br.height === 0 && (br.left !== 0 || br.top !== 0)) {
    const node = range.startContainer;
    const el = node.nodeType === Node.ELEMENT_NODE ? (node as HTMLElement) : node.parentElement;
    if (el) {
      const style = getComputedStyle(el);
      let lineH = parseFloat(style.lineHeight);
      if (isNaN(lineH)) lineH = parseFloat(style.fontSize) * 1.2;
      return DOMRect.fromRect?.({ x: br.left, y: br.top, width: 0, height: lineH || 16 }) ??
        new DOMRect(br.left, br.top, 0, lineH || 16);
    }
  }
  return null;
}

export function ExpressionRichEditor({
  value,
  onChange,
  domainId,
  publishedOnly = true,
  placeholder,
  disabled,
  minHeight = 160,
}: Props) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const lastRenderedKeyRef = useRef<string>("");
  const refCounterRef = useRef<number>(1);
  const triggerRef = useRef<Trigger | null>(null);
  const composingRef = useRef<boolean>(false);
  const onChangeRef = useRef<typeof onChange>(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  const [objects, setObjects] = useState<ObjectTypeSummary[]>([]);
  const [objectsLoading, setObjectsLoading] = useState(false);
  const [propCache, setPropCache] = useState<Record<string, Property[]>>({});
  const [propsLoading, setPropsLoading] = useState(false);
  const [popup, setPopup] = useState<Trigger | null>(null);
  const [highlight, setHighlight] = useState(0);

  // 虚拟参考元素: 每次调用 getBoundingClientRect 时实时获取光标坐标
  const virtualRef = useRef({
    getBoundingClientRect(): DOMRect {
      const r = getCaretRect();
      return r ?? new DOMRect(0, 0, 0, 0);
    },
  });

  const { refs, floatingStyles, update } = useFloating({
    placement: "bottom-start",
    middleware: [offset(2), flip({ padding: 8 }), shift({ padding: 8 })],
  });

  // popup 出现/变化时同步虚拟参考元素并刷新位置
  useEffect(() => {
    if (popup) {
      refs.setReference(virtualRef.current);
      requestAnimationFrame(() => update());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popup?.kind, popup?.query, refs, update]);

  const syncToParent = () => {
    const el = editorRef.current;
    if (!el) return;
    const segments = domToSegments(el);
    const draft: ExpressionDraft = { segments };
    lastRenderedKeyRef.current = JSON.stringify(draft);
    onChangeRef.current?.(draft);
  };

  // 每次渲染 chip 删除回调需要重新绑定 —— 用闭包引用 editorRef 即可
  const handleChipRemove = () => {
    requestAnimationFrame(() => {
      syncToParent();
    });
  };

  // 初始化 / 外部 value 变化时,重新渲染 innerHTML
  useLayoutEffect(() => {
    const el = editorRef.current;
    if (!el) return;
    const draft = value ?? { segments: [] };
    const key = JSON.stringify(draft);
    if (key === lastRenderedKeyRef.current) return;
    lastRenderedKeyRef.current = key;
    el.innerHTML = segmentsToHtml(draft.segments ?? [], handleChipRemove);
    // 重新绑定 chip 内的删除回调(innerHTML 重建后丢失事件监听)
    bindChipRemoveHandlers(el, handleChipRemove);
    // 更新 ref 计数器,避免新插入 chip 时 ref_id 与已有冲突
    refCounterRef.current = Math.max(refCounterRef.current, countExistingRefs(el) + 1);
  }, [value]);

  useEffect(() => {
    if (!domainId) {
      setObjects([]);
      return;
    }
    setObjectsLoading(true);
    api
      .listObjectTypes({ domainId, publishedOnly })
      .then(setObjects)
      .catch(() => setObjects([]))
      .finally(() => setObjectsLoading(false));
  }, [domainId, publishedOnly]);

  // 当 popup 指向某对象时,懒加载该对象属性
  useEffect(() => {
    if (!popup || popup.kind !== "property") return;
    const objId = popup.objectTypeId;
    if (propCache[objId]) return;
    setPropsLoading(true);
    api
      .getObjectType(objId)
      .then((detail) => {
        setPropCache((prev) => ({ ...prev, [objId]: detail.properties ?? [] }));
      })
      .catch(() => {
        setPropCache((prev) => ({ ...prev, [objId]: [] }));
      })
      .finally(() => setPropsLoading(false));
  }, [popup, propCache]);

  const filteredObjects = useMemo(() => {
    if (!popup || popup.kind !== "object") return [];
    const q = popup.query.toLowerCase().trim();
    return objects
      .filter(
        (o) =>
          !q ||
          o.name.toLowerCase().includes(q) ||
          o.display_name.toLowerCase().includes(q),
      )
      .slice(0, 8);
  }, [popup, objects]);

  const filteredProperties = useMemo(() => {
    if (!popup || popup.kind !== "property") return [];
    const props = propCache[popup.objectTypeId] ?? [];
    const q = popup.query.toLowerCase().trim();
    return props
      .filter(
        (p) =>
          !q ||
          p.name.toLowerCase().includes(q) ||
          p.display_name.toLowerCase().includes(q),
      )
      .slice(0, 8);
  }, [popup, propCache]);

  useEffect(() => {
    setHighlight(0);
  }, [popup?.kind, popup?.query, popup && popup.kind === "property" ? popup.objectTypeId : undefined]);

  const closePopup = () => {
    triggerRef.current = null;
    setPopup(null);
  };

  const updatePopupFromCaret = () => {
    const el = editorRef.current;
    if (!el) return;
    const next = detectTrigger();
    triggerRef.current = next;
    setPopup(next);
  };

  const handleInput = () => {
    if (composingRef.current) return;
    syncToParent();
    updatePopupFromCaret();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (composingRef.current) return;
    if (e.key === "Escape" && popup) {
      e.preventDefault();
      closePopup();
      return;
    }
    if (popup) {
      const list = popup.kind === "object" ? filteredObjects : filteredProperties;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((h) => (list.length === 0 ? 0 : (h + 1) % list.length));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((h) => (list.length === 0 ? 0 : (h - 1 + list.length) % list.length));
        return;
      }
      if (e.key === "Enter" && list.length > 0) {
        e.preventDefault();
        pickCurrent();
        return;
      }
    }
  };

  const pickCurrent = () => {
    if (!popup) return;
    if (popup.kind === "object") {
      const target = filteredObjects[highlight];
      if (target) insertObjectChip(target);
    } else {
      const target = filteredProperties[highlight];
      if (target) extendChipWithProperty(popup, target);
    }
  };

  const insertObjectChip = (obj: ObjectTypeSummary) => {
    const trigger = triggerRef.current;
    const el = editorRef.current;
    if (!el || !trigger || trigger.kind !== "object") return;
    // 删除 `@` + 过滤文本
    const range = document.createRange();
    range.setStart(trigger.node, trigger.atOffset);
    range.setEnd(trigger.node, trigger.endOffset);
    range.deleteContents();
    // 插入 chip
    const refId = genRefId(refCounterRef.current);
    refCounterRef.current += 1;
    const seg: ExpressionRefSegment = {
      type: "ref",
      ref_id: refId,
      object_type_id: obj.id,
      object_name: obj.name,
      object_display_name: obj.display_name,
    };
    const chip = createChipEl(seg, handleChipRemove);
    range.insertNode(chip);
    // 光标移到 chip 之后
    const after = document.createRange();
    after.setStartAfter(chip);
    after.collapse(true);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(after);
    closePopup();
    syncToParent();
    // 插入对象后,若用户接着输入 `.` 会自动触发属性 picker
    el.focus();
  };

  const extendChipWithProperty = (trigger: PropertyTrigger, prop: Property) => {
    const el = editorRef.current;
    if (!el) return;
    // 删除 `.` + 过滤文本
    const range = document.createRange();
    range.setStart(trigger.node, trigger.atOffset);
    range.setEnd(trigger.node, trigger.endOffset);
    range.deleteContents();
    // 更新 chip 元素的 dataset 与标签
    const chipEl = trigger.chipEl;
    chipEl.dataset.propertyId = prop.id;
    chipEl.dataset.propertyName = prop.name;
    chipEl.dataset.propertyDisplayName = prop.display_name || prop.name;
    chipEl.classList.remove("expr-chip--object");
    chipEl.classList.add("expr-chip--property");
    const labelEl = chipEl.querySelector(".expr-chip__label");
    if (labelEl) {
      const objDisplay = chipEl.dataset.objectDisplayName || chipEl.dataset.objectName || "?";
      labelEl.textContent = `${objDisplay}.${prop.display_name || prop.name}`;
    }
    // 光标移到 chip 之后
    const after = document.createRange();
    after.setStartAfter(chipEl);
    after.collapse(true);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(after);
    closePopup();
    syncToParent();
    el.focus();
  };

  const handleCompositionStart = () => {
    composingRef.current = true;
  };
  const handleCompositionEnd = () => {
    composingRef.current = false;
    handleInput();
  };

  const handleBlur = () => {
    // 延迟关闭,允许点击 popup 项
    setTimeout(() => {
      const el = editorRef.current;
      if (el && document.activeElement === el) return;
      closePopup();
    }, 200);
  };

  const writeSegmentsToClipboard = (
    e: React.ClipboardEvent,
    segments: ExpressionSegment[],
  ) => {
    e.clipboardData.setData(EXPRESSION_CLIPBOARD_TYPE, JSON.stringify(segments));
    e.clipboardData.setData(
      "text/plain",
      segments
        .map((seg) => {
          if (seg.type === "text") return seg.value;
          const obj = seg.object_display_name || seg.object_name || "?";
          if (seg.property_id)
            return `@${obj}.${seg.property_display_name || seg.property_name || "?"}`;
          return `@${obj}`;
        })
        .join(""),
    );
    e.preventDefault();
  };

  const handleCopy = (e: React.ClipboardEvent) => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (range.collapsed) return;
    const segments = fragmentToSegments(range.cloneContents());
    if (segments.length === 0) return;
    writeSegmentsToClipboard(e, segments);
  };

  const handleCut = (e: React.ClipboardEvent) => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (range.collapsed) return;
    const segments = fragmentToSegments(range.cloneContents());
    if (segments.length === 0) return;
    writeSegmentsToClipboard(e, segments);
    range.deleteContents();
    syncToParent();
  };

  const insertTextAtCursor = (text: string) => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    range.deleteContents();
    const textNode = document.createTextNode(text);
    range.insertNode(textNode);
    // insertNode 之后 range 已 collapsed 到插入节点之后
    sel.removeAllRanges();
    sel.addRange(range);
    syncToParent();
  };

  const insertSegmentsAtCursor = (segments: ExpressionSegment[]) => {
    const el = editorRef.current;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    range.deleteContents();

    const fragment = document.createDocumentFragment();
    for (const seg of segments) {
      if (seg.type === "text") {
        fragment.appendChild(document.createTextNode(seg.value));
      } else {
        const chip = createChipEl(seg, handleChipRemove);
        fragment.appendChild(chip);
      }
    }

    range.insertNode(fragment);
    // DocumentFragment 在 insertNode 后子节点已移入 DOM,range 自动 collapsed 到末尾
    sel.removeAllRanges();
    sel.addRange(range);

    refCounterRef.current = Math.max(
      refCounterRef.current,
      countExistingRefs(el) + 1,
    );
    syncToParent();
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    // 优先使用自定义格式恢复 chip 完整结构
    const jsonData = e.clipboardData.getData(EXPRESSION_CLIPBOARD_TYPE);
    if (jsonData) {
      e.preventDefault();
      try {
        insertSegmentsAtCursor(JSON.parse(jsonData) as ExpressionSegment[]);
        return;
      } catch {
        // JSON 解析失败 → 回退到纯文本粘贴
      }
    }

    // 无自定义格式:只粘贴纯文本,避免 HTML 携带外部标签污染编辑器
    e.preventDefault();
    const text = e.clipboardData.getData("text/plain");
    if (!text) {
      // 尝试取 text/html 兜底(如从外部 HTML 复制)
      const html = e.clipboardData.getData("text/html");
      if (html) {
        const tmp = document.createElement("div");
        tmp.innerHTML = html;
        insertTextAtCursor(tmp.textContent || "");
      }
      return;
    }
    insertTextAtCursor(text);
  };

  const showObjectPopup = popup?.kind === "object" && filteredObjects.length > 0;
  const showPropertyPopup = popup?.kind === "property" && filteredProperties.length > 0;
  const showPropsLoading =
    popup?.kind === "property" && propsLoading && filteredProperties.length === 0;

  return (
    <div className="expression-editor expression-editor--rich">
      <div
        ref={editorRef}
        className="expression-editor__content"
        contentEditable={!disabled}
        suppressContentEditableWarning
        onInput={handleInput}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        onCopy={handleCopy}
        onCut={handleCut}
        onPaste={handlePaste}
        onCompositionStart={handleCompositionStart}
        onCompositionEnd={handleCompositionEnd}
        data-placeholder={placeholder}
        style={{ minHeight }}
      />
      {showObjectPopup &&
        createPortal(
          <div ref={refs.setFloating} className="expr-popup" style={floatingStyles} role="listbox">
            <div className="expr-popup__head">
              <span className="expr-popup__head-title">对象</span>
              <span className="expr-popup__head-meta">{filteredObjects.length} 项</span>
            </div>
            {objectsLoading && (
              <div className="expr-popup__loading">
                <Spin size="small" />
              </div>
            )}
            {filteredObjects.map((o, idx) => (
              <button
                key={o.id}
                type="button"
                className={`expr-popup__item${idx === highlight ? " is-active" : ""}`}
                onMouseEnter={() => setHighlight(idx)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  setHighlight(idx);
                  insertObjectChip(o);
                }}
              >
                <span className="expr-sigil expr-sigil--object" aria-hidden>
                  T
                </span>
                <span className="expr-popup__item-main">
                  <span className="expr-popup__item-name">{o.display_name}</span>
                  {o.description && (
                    <span className="expr-popup__item-desc">{o.description}</span>
                  )}
                </span>
                <span className="expr-popup__item-sub">{o.name}</span>
              </button>
            ))}
          </div>,
          document.body,
        )}
      {showPropertyPopup &&
        createPortal(
          <div ref={refs.setFloating} className="expr-popup" style={floatingStyles} role="listbox">
            <div className="expr-popup__head">
              <span className="expr-popup__head-title">属性</span>
              <span className="expr-popup__head-meta">{filteredProperties.length} 项</span>
            </div>
            {filteredProperties.map((p, idx) => (
              <button
                key={p.id}
                type="button"
                className={`expr-popup__item${idx === highlight ? " is-active" : ""}`}
                onMouseEnter={() => setHighlight(idx)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  setHighlight(idx);
                  if (popup && popup.kind === "property") extendChipWithProperty(popup, p);
                }}
              >
                <span className="expr-sigil expr-sigil--property" aria-hidden>
                  P
                </span>
                <span className="expr-popup__item-main">
                  <span className="expr-popup__item-name">{p.display_name}</span>
                  {p.description && (
                    <span className="expr-popup__item-desc">{p.description}</span>
                  )}
                </span>
                <span className="expr-popup__item-tail">
                  {p.data_type && (
                    <span className="expr-popup__item-type">{p.data_type}</span>
                  )}
                  <span className="expr-popup__item-sub">{p.name}</span>
                </span>
              </button>
            ))}
          </div>,
          document.body,
        )}
      {showPropsLoading &&
        createPortal(
          <div ref={refs.setFloating} className="expr-popup expr-popup--hint" style={floatingStyles}>
            <Spin size="small" /> <span className="om-muted">加载属性…</span>
          </div>,
          document.body,
        )}
      {!showObjectPopup && !showPropertyPopup && !showPropsLoading && popup && (
        <div className="expression-editor__hint">
          <span className="om-muted">
            {popup.kind === "object"
              ? "未找到匹配的对象"
              : "未找到匹配的字段,或该对象尚未加载属性"}
          </span>
        </div>
      )}
    </div>
  );
}

function bindChipRemoveHandlers(root: HTMLElement, onRemove: () => void) {
  root.querySelectorAll<HTMLElement>(".expr-chip").forEach((chip) => {
    const btn = chip.querySelector<HTMLButtonElement>(".expr-chip__remove");
    if (btn) {
      // 替换为新的按钮以清除旧监听
      const fresh = document.createElement("button");
      fresh.type = "button";
      fresh.className = "expr-chip__remove";
      fresh.setAttribute("aria-label", "移除引用");
      fresh.textContent = "×";
      fresh.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        chip.remove();
        onRemove();
      });
      btn.replaceWith(fresh);
    }
  });
}

function countExistingRefs(root: HTMLElement): number {
  return root.querySelectorAll("[data-ref-id]").length;
}

const EXPRESSION_CLIPBOARD_TYPE = "application/x-ontometa-expression";

function fragmentToSegments(fragment: DocumentFragment): ExpressionSegment[] {
  const out: ExpressionSegment[] = [];
  let textBuf = "";
  const flush = () => {
    if (textBuf) {
      out.push({ type: "text", value: textBuf });
      textBuf = "";
    }
  };
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      textBuf += node.textContent || "";
      return;
    }
    if (node.nodeName === "BR") {
      textBuf += "\n";
      return;
    }
    const el = node as HTMLElement;
    if (el.dataset?.refId) {
      flush();
      out.push({
        type: "ref",
        ref_id: el.dataset.refId,
        object_type_id: el.dataset.objectTypeId || "",
        object_name: el.dataset.objectName || "",
        object_display_name: el.dataset.objectDisplayName || "",
        property_id: el.dataset.propertyId || undefined,
        property_name: el.dataset.propertyName || undefined,
        property_display_name: el.dataset.propertyDisplayName || undefined,
      });
      return;
    }
    for (const child of node.childNodes) {
      walk(child);
    }
  };
  for (const child of fragment.childNodes) {
    walk(child);
  }
  flush();
  return out;
}
