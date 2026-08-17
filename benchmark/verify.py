"""投递后的一致性校验 + manifest 生成（README 步骤 4）。

直连两个库跑 SQL，不走应用 API——聚合校验用 REST 既慢又容易被分页坑。

**跑不了的检查一律记 SKIPPED 并让整体失败**，不当成通过。缺个驱动就静默少跑两项、
最后打一行「全部通过」，正是这套验证方案从头到尾在防的假绿。

不通过就改生成器、恢复 baseline 快照重跑，**不要手工补数据**——手工补的行不进
truth.json，真值一旦和实际脱节，后面所有交叉校验就都失效了。

用法::

    pip install pymysql psycopg2-binary
    python verify.py --truth /srv/ontometa/benchmark/truth.json \\
      --erp-dsn  mysql://datahub_ro:pwd@localhost:3306/_erpnext \\
      --odoo-dsn postgres://datahub_ro:pwd@localhost:5432/odoo_o2c \\
      --manifest /srv/ontometa/benchmark/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
TOLERANCE = Decimal("0.05")  # 金额比对容差：两侧四舍五入位置不同会有分位差


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(Result(name, status, detail))

    def check(self, name: str, actual: Any, expected: Any) -> None:
        if isinstance(expected, Decimal) or isinstance(actual, Decimal):
            a, e = Decimal(str(actual or 0)), Decimal(str(expected or 0))
            ok = abs(a - e) <= TOLERANCE
        else:
            ok = actual == expected
        self.add(name, PASS if ok else FAIL, f"实际 {actual} / 预期 {expected}")

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status != PASS]

    def render(self) -> str:
        width = max(len(r.name) for r in self.results) if self.results else 20
        lines = [f"  {r.status:4}  {r.name:<{width}}  {r.detail}" for r in self.results]
        return "\n".join(lines)


# ---------------------------------------------------------------- 连接

def _parse(dsn: str) -> dict[str, Any]:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "localhost",
        "port": u.port,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/"),
    }


class Sql:
    """极薄的只读查询封装；驱动缺失时 available=False，调用方据此记 SKIP。"""

    def __init__(self, dsn: str | None, kind: str):
        self.kind, self.error, self.conn = kind, None, None
        if not dsn:
            self.error = "未提供 DSN"
            return
        cfg = _parse(dsn)
        try:
            if kind == "mysql":
                import pymysql

                self.conn = pymysql.connect(port=cfg["port"] or 3306, charset="utf8mb4", **{k: v for k, v in cfg.items() if k != "port"})
            else:
                import psycopg2

                self.conn = psycopg2.connect(
                    host=cfg["host"], port=cfg["port"] or 5432, user=cfg["user"],
                    password=cfg["password"], dbname=cfg["database"],
                )
        except ImportError as exc:
            self.error = f"缺驱动：{exc}（pip install pymysql psycopg2-binary）"
        except Exception as exc:  # noqa: BLE001
            self.error = f"连接失败：{str(exc)[:200]}"

    @property
    def available(self) -> bool:
        return self.conn is not None

    def scalar(self, sql: str) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None

    def rows(self, sql: str) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())


# ---------------------------------------------------------------- 各项检查

def check_erp(sql: Sql, truth: dict, rep: Report) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not sql.available:
        rep.add("ERPNext 全部检查", SKIP, sql.error or "")
        return counts
    exp = truth["expected_erp"]

    q = {
        "sales_order_rows": "SELECT COUNT(*) FROM `tabSales Order`",
        "sales_order_submitted": "SELECT COUNT(*) FROM `tabSales Order` WHERE docstatus=1",
        "sales_order_cancelled": "SELECT COUNT(*) FROM `tabSales Order` WHERE docstatus=2",
        "sales_order_item_rows": "SELECT COUNT(*) FROM `tabSales Order Item`",
        "delivery_note_rows": "SELECT COUNT(*) FROM `tabDelivery Note` WHERE docstatus=1",
        "sales_invoice_rows": "SELECT COUNT(*) FROM `tabSales Invoice` WHERE docstatus=1",
        "payment_entry_rows": "SELECT COUNT(*) FROM `tabPayment Entry` WHERE docstatus=1",
        "customer_rows": "SELECT COUNT(*) FROM `tabCustomer`",
        "item_rows": "SELECT COUNT(*) FROM `tabItem`",
    }
    for key, stmt in q.items():
        actual = sql.scalar(stmt)
        counts[key] = actual
        rep.check(f"ERP {key}", actual, exp[key])

    total = sql.scalar("SELECT COALESCE(SUM(base_grand_total),0) FROM `tabSales Order` WHERE docstatus=1")
    counts["submitted_grand_total"] = float(total or 0)
    rep.check("ERP 提交态订单金额合计", Decimal(str(total or 0)), Decimal(exp["submitted_grand_total"]))

    # 尾差脏案例：挂了整单折扣的订单数应等于配方值
    residue = sql.scalar("SELECT COUNT(*) FROM `tabSales Order` WHERE docstatus=1 AND discount_amount > 0")
    rep.check("ERP 尾差订单数（表头≠行合计）", residue, truth["dirty_cases"]["rounding_residue"])

    # GL 借贷必须平
    diff = sql.scalar("SELECT COALESCE(SUM(debit),0)-COALESCE(SUM(credit),0) FROM `tabGL Entry` WHERE is_cancelled=0")
    rep.add("ERP 总账借贷平衡", PASS if abs(Decimal(str(diff or 0))) <= TOLERANCE else FAIL, f"差额 {diff}")

    # 库存台账累计应等于 Bin 结存
    bad = sql.rows(
        "SELECT b.item_code, b.warehouse, b.actual_qty, COALESCE(s.q,0) FROM `tabBin` b "
        "LEFT JOIN (SELECT item_code, warehouse, SUM(actual_qty) q FROM `tabStock Ledger Entry` "
        "WHERE is_cancelled=0 GROUP BY item_code, warehouse) s "
        "ON s.item_code=b.item_code AND s.warehouse=b.warehouse "
        "WHERE ABS(b.actual_qty-COALESCE(s.q,0)) > 0.001 LIMIT 5"
    )
    rep.add("ERP 库存台账与结存一致", PASS if not bad else FAIL, f"不一致 {len(bad)} 处 {bad[:2]}")
    return counts


def check_odoo(sql: Sql, truth: dict, rep: Report) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not sql.available:
        rep.add("Odoo 全部检查", SKIP, sql.error or "")
        return counts
    exp = truth["expected_odoo"]

    q = {
        "sale_order_rows": "SELECT COUNT(*) FROM sale_order",
        "sale_order_cancelled": "SELECT COUNT(*) FROM sale_order WHERE state='cancel'",
        "sale_order_confirmed": "SELECT COUNT(*) FROM sale_order WHERE state IN ('sale','done')",
        "order_line_rows": "SELECT COUNT(*) FROM sale_order_line",
        "partner_rows": "SELECT COUNT(*) FROM res_partner WHERE vat IS NOT NULL",
        "product_product_rows": "SELECT COUNT(*) FROM product_product WHERE default_code IS NOT NULL",
        "product_template_rows": "SELECT COUNT(*) FROM product_template",
    }
    for key, stmt in q.items():
        actual = sql.scalar(stmt)
        counts[key] = actual
        rep.check(f"Odoo {key}", actual, exp[key])

    untaxed = sql.scalar("SELECT COALESCE(SUM(amount_untaxed),0) FROM sale_order")
    counts["amount_untaxed_sum"] = float(untaxed or 0)
    rep.check("Odoo 未税金额合计", Decimal(str(untaxed or 0)), Decimal(exp["amount_untaxed_sum"]))
    return counts


def check_conflicts(erp: Sql, odoo: Sql, truth: dict, rep: Report) -> None:
    """冲突注入是否真的生效——这几条直接决定跨系统题有没有答案。"""
    conflicts = truth["conflicts"]

    if not erp.available or not odoo.available:
        rep.add("跨系统冲突检查", SKIP, "需要两侧都连得上")
        return

    # ① 未下传的单：Odoo 有、ERP 无
    missing = [m["po_no"] for m in conflicts["orders_missing_in_erp"]]
    if missing:
        quoted = ",".join("'" + p.replace("'", "''") + "'" for p in missing)
        in_erp = erp.scalar(f"SELECT COUNT(*) FROM `tabSales Order` WHERE po_no IN ({quoted})")
        in_odoo = odoo.scalar(f"SELECT COUNT(*) FROM sale_order WHERE client_order_ref IN ({quoted})")
        rep.check("未下传单在 ERP 中不存在", in_erp, 0)
        rep.check("未下传单在 Odoo 中存在", in_odoo, len(missing))

    # ② 共享客户两边都在，且难匹配那档确实靠名字对不上
    shared = conflicts["shared_customers"]
    hard = [c for c in shared if c["match_kind"] == "attr_only"]
    if hard:
        vats = ",".join("'" + c["tax_id"] + "'" for c in hard)
        erp_n = erp.scalar(f"SELECT COUNT(*) FROM `tabCustomer` WHERE tax_id IN ({vats})")
        odoo_n = odoo.scalar(f"SELECT COUNT(*) FROM res_partner WHERE vat IN ({vats})")
        rep.check("难匹配客户在 ERP 存在", erp_n, len(hard))
        rep.check("难匹配客户在 Odoo 存在", odoo_n, len(hard))
        # 名称必须对不上，否则这档冲突白造了
        names = ",".join("'" + c["odoo_name"].replace("'", "''") + "'" for c in hard)
        collide = erp.scalar(f"SELECT COUNT(*) FROM `tabCustomer` WHERE customer_name IN ({names})")
        rep.check("难匹配客户名称确实对不上", collide, 0)


# ---------------------------------------------------------------- manifest

def write_manifest(path: Path, truth_path: Path, truth: dict, erp: dict, odoo: dict, ok: bool) -> None:
    digest = hashlib.sha256(truth_path.read_bytes()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "verified": ok,
                "seed": truth["meta"]["seed"],
                "window": truth["meta"]["window"],
                "truth_file": str(truth_path),
                "truth_sha256": digest,
                "erp_row_counts": erp,
                "odoo_row_counts": odoo,
                # 镜像 digest 与 ERPNext/Odoo 版本号请在打快照时补进来：
                # 半个月后重建环境，没有它就说不清拿到的是不是同一套 schema
                "images": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    p = argparse.ArgumentParser(description="投递后一致性校验（README 步骤 4）")
    p.add_argument("--truth", required=True, help="truth.json 路径")
    p.add_argument("--erp-dsn", help="mysql://user:pwd@host:3306/_erpnext")
    p.add_argument("--odoo-dsn", help="postgres://user:pwd@host:5432/odoo_o2c")
    p.add_argument("--manifest", help="通过后写 manifest.json 到此路径")
    args = p.parse_args()

    truth_path = Path(args.truth)
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if "expected_erp" not in truth:
        print("!! truth.json 里没有 expected_erp —— 它是旧版生成器产出的，重新生成一份")
        return 2

    rep = Report()
    erp, odoo = Sql(args.erp_dsn, "mysql"), Sql(args.odoo_dsn, "postgres")
    erp_counts = check_erp(erp, truth, rep)
    odoo_counts = check_odoo(odoo, truth, rep)
    check_conflicts(erp, odoo, truth, rep)

    print(rep.render())
    bad = rep.failed
    ok = not bad
    if args.manifest:
        write_manifest(Path(args.manifest), truth_path, truth, erp_counts, odoo_counts, ok)
        print(f"\nmanifest 已写入 {args.manifest}")

    if ok:
        print(f"\n== 全部 {len(rep.results)} 项通过，可以打数据快照了 ==")
        return 0
    skipped = [r for r in bad if r.status == SKIP]
    print(f"\n!! {len(bad)} 项未通过（其中 {len(skipped)} 项是跳过）")
    if skipped:
        print("   跳过不等于通过：补上驱动或 DSN 再跑一遍，别拿这份结果当验收依据。")
    print("   修生成器 → 恢复 baseline 快照 → 重跑。不要手工补数据。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
