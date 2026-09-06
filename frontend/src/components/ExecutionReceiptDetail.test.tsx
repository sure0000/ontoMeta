import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ExecutionReceiptDetail } from "./AgentsPanel";

/**
 * 落数验证的三态展示。
 *
 * 审计里最刺眼的一条就出在这里：同步搬了 0 行，界面照样显示绿色的
 * 「Doris 落数验证通过 · 共 0 行」。后端当时其实已经在回执里写了 `empty: true`，
 * 只是这一层没渲染——所以这组用例盯的是**展示层有没有把结论说全**，
 * 后端的判定逻辑另有 backend/tests/test_sync_reconciliation.py 盯着。
 */

function renderReceipt(verification: Record<string, unknown>) {
  return render(
    <ExecutionReceiptDetail receipt={{ doris_verification: verification }} liveState={null} />,
  );
}

describe("落数验证的展示", () => {
  it("源与目标行数一致时给出无保留的通过", () => {
    renderReceipt({
      verified: true,
      status: "verified",
      target_table: "ods.ods_erpnext_tab_customer",
      row_count: 500,
      source_table: "erp.tabCustomer",
      source_row_count: 500,
      comparison: "match",
      caveat: null,
    });

    expect(screen.getByText("Doris 落数验证通过")).toBeInTheDocument();
    // 两边的数字都要露出来，用户不必自己去两处核对
    expect(screen.getByText(/500 行/)).toBeInTheDocument();
  });

  it("0 行不给纯绿勾，并说清它意味着什么", () => {
    renderReceipt({
      verified: true,
      status: "verified",
      target_table: "ods.ods_erpnext_tab_code_list",
      row_count: 0,
      source_row_count: 0,
      comparison: "match",
      empty: true,
      caveat: "源表也是 0 行，目标表建好了但没有数据",
    });

    expect(screen.getByText("Doris 落数验证通过（有保留）")).toBeInTheDocument();
    expect(screen.getByText(/没有数据/)).toBeInTheDocument();
    expect(screen.queryByText("Doris 落数验证通过")).not.toBeInTheDocument();
  });

  it("没能与源核对时同样带保留", () => {
    renderReceipt({
      verified: true,
      status: "verified",
      target_table: "ods.t",
      row_count: 42,
      source_row_count: null,
      comparison: "unavailable",
      caveat: "未能统计源表行数，本次没有做行数核对（源库连接被拒绝）",
    });

    expect(screen.getByText("Doris 落数验证通过（有保留）")).toBeInTheDocument();
    expect(screen.getByText(/没有做行数核对/)).toBeInTheDocument();
  });

  it("行数对不上按失败展示，并把两个数字都给出来", () => {
    renderReceipt({
      verified: false,
      status: "failed",
      target_table: "ods.t",
      row_count: 50,
      source_row_count: 500,
      comparison: "mismatch",
      error: "落数行数与源表不一致：源 500 行，目标 50 行。目标表当前是一份不完整的数据，请查 Flink 作业日志后重跑。",
    });

    expect(screen.getByText("Doris 落数验证失败")).toBeInTheDocument();
    expect(screen.getByText(/源 500 行，目标 50 行/)).toBeInTheDocument();
  });
});

describe("存量回执（改动之前跑的任务）", () => {
  it("没有 caveat 字段但 0 行的，展示层自己补一句", () => {
    // 历史回执只有 row_count，没有 source_row_count / comparison / caveat。
    // 不补的话「修了判定逻辑却只对新任务生效」——库里那批 0 行任务照旧顶着绿勾。
    renderReceipt({
      verified: true,
      status: "verified",
      target_table: "ods.ods_erpnext_tab_stock_closing_entry",
      row_count: 0,
      empty: true,
    });

    expect(screen.getByText("Doris 落数验证通过（有保留）")).toBeInTheDocument();
    expect(screen.getByText(/早于行数核对上线/)).toBeInTheDocument();
  });

  it("存量回执里有行数的仍是干净的通过", () => {
    renderReceipt({
      verified: true,
      status: "verified",
      target_table: "ods.ods_erpnext_tab_customer",
      row_count: 500,
    });

    expect(screen.getByText("Doris 落数验证通过")).toBeInTheDocument();
  });
});
