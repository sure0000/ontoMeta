import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ToolCatalog } from "./McpPanel";
import type { McpToolCategory, McpToolInfo } from "../types";

/**
 * 工具目录的分组展示。
 *
 * 73 个工具曾经是一张按英文名排序的平表——`advance_task_flow` 紧挨着
 * `analyze_query`，既看不出这两个东西毫不相干，也没有一个字告诉人它们是干嘛的。
 * 所以这组用例盯的是「分组有没有真的显示出来」：中文名在场、同类合并成一格、
 * 筛完之后合并格跟着重算（后者是 rowSpan 最容易错位的地方）。
 */

const CATEGORIES: McpToolCategory[] = [
  { key: "ontology", label: "本体与对象", tool_count: 2 },
  { key: "query", label: "取数与分析", tool_count: 1 },
];

const TOOLS: McpToolInfo[] = [
  {
    name: "query_objects",
    display_name: "业务对象查询",
    category: "ontology",
    category_label: "本体与对象",
    description: "查询本体中的业务对象",
    required_role: "reader",
  },
  {
    name: "query_relations",
    display_name: "对象关系查询",
    category: "ontology",
    category_label: "本体与对象",
    description: "查询本体中的业务对象关系",
    required_role: "reader",
  },
  {
    name: "execute_sql",
    display_name: "执行只读 SQL",
    category: "query",
    category_label: "取数与分析",
    description: "在默认 Doris 数仓执行只读 SQL",
    required_role: "publisher",
  },
];

function renderCatalog() {
  return render(<ToolCatalog tools={TOOLS} categories={CATEGORIES} loading={false} />);
}

/** 「分类」列里真正渲染出来的格子（被 rowSpan 吃掉的行不会有格子）。 */
function categoryCells() {
  return screen
    .getAllByRole("row")
    .slice(1) // 表头
    .map((row) => within(row).getAllByRole("cell"))
    .filter((cells) => cells.length === 4)
    .map((cells) => cells[0].textContent);
}

describe("MCP 工具目录", () => {
  it("每个工具同时给中文名和英文标识名", () => {
    renderCatalog();
    expect(screen.getByText("业务对象查询")).toBeInTheDocument();
    expect(screen.getByText("query_objects")).toBeInTheDocument();
  });

  it("同一分类的相邻行合并成一格", () => {
    renderCatalog();
    // 三行工具、两个分类：本体那格跨两行，所以只剩两个分类格。
    expect(categoryCells()).toEqual(["本体与对象", "取数与分析"]);
  });

  it("按分类筛选后只留该类", async () => {
    renderCatalog();
    await userEvent.click(screen.getByText(/取数与分析 1/));
    expect(screen.getByText("执行只读 SQL")).toBeInTheDocument();
    expect(screen.queryByText("业务对象查询")).not.toBeInTheDocument();
  });

  it("按角色筛掉中间行后，合并格按筛后的行重算", async () => {
    renderCatalog();
    await userEvent.click(screen.getByText(/取数与分析 1/));
    await userEvent.click(screen.getByText(/全部 3/));
    // 只看 publisher：本体那两行全没了，剩一行 execute_sql，合并格不能还跨着两行。
    const [roleSelect] = screen.getAllByRole("combobox");
    await userEvent.click(roleSelect);
    await userEvent.click(await screen.findByTitle("publisher"));
    expect(categoryCells()).toEqual(["取数与分析"]);
  });
});
