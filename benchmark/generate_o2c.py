"""O2C 造数入口：生成中立台账 → 落 truth.json → 双向投递。

配方见 ``docs/BENCHMARK_DATA_PREP.md`` §3，执行步骤见 ``deploy/benchmark/README.md`` 步骤 3。

用法（在 docker 宿主机上，跑在宿主机而非容器里——它只发 HTTP/XML-RPC）::

    python3 -m venv ~/bench-venv && source ~/bench-venv/bin/activate && pip install requests

    # 只生成台账与真值，不连任何系统（先跑这个确认配方与真值合理）
    python generate_o2c.py --only ledger --out /srv/ontometa/benchmark/truth.json

    # 小规模冒烟，确认两个投递器接得上（几分钟）
    python generate_o2c.py --orders 40 --online-orders 15 \\
      --erp http://localhost:8090 --erp-key <key>:<secret> \\
      --odoo http://localhost:8069 --odoo-db odoo_o2c --odoo-password <pwd> \\
      --out /tmp/truth-smoke.json

    # 全量（3-5 小时，跑一夜）
    nohup python generate_o2c.py --erp ... --odoo ... \\
      --out /srv/ontometa/benchmark/truth.json > /srv/ontometa/benchmark/gen.log 2>&1 &

**不支持断点续跑**：中途失败就从 baseline 快照恢复两个库后重跑。做成可续跑要在两侧
维护幂等键，成本远高于重跑一夜——而且半截数据混着重试痕迹，比重来更难排查。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from generator.config import DEFAULT, Recipe
from generator.ledger import build_ledger
from generator.truth import dump_truth


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_env_file() -> None:
    """读同目录的 .env（已被仓库 .gitignore 覆盖）。

    凭据走文件而不是命令行：``--erp-key`` 会出现在 shell 历史和 ps 输出里，
    一台多人用的机器上这就是泄漏。命令行参数仍然可用，优先级更高。
    """
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def main() -> int:
    p = argparse.ArgumentParser(description="生成 O2C 验证语料（台账 + 真值 + 双向投递）")
    p.add_argument("--only", choices=("ledger", "erp", "odoo", "all"), default="all")
    p.add_argument("--out", default="truth.json", help="truth.json 落盘路径")
    p.add_argument("--seed", type=int, default=DEFAULT.seed)
    p.add_argument("--concurrency", type=int, default=DEFAULT.concurrency,
                   help="并发提交数；ERPNext 侧应与 gunicorn worker 数一致，开更大只会排队")
    # 规模覆盖（冒烟用）
    p.add_argument("--orders", type=int)
    p.add_argument("--online-orders", type=int)
    p.add_argument("--customers", type=int)
    p.add_argument("--skus", type=int)
    # ERPNext
    p.add_argument("--erp", help="ERPNext base url，如 http://localhost:8090")
    p.add_argument("--erp-key", help="API key:secret")
    p.add_argument("--erp-company")
    p.add_argument("--erp-warehouse")
    # Odoo
    p.add_argument("--odoo", help="Odoo base url，如 http://localhost:8069")
    p.add_argument("--odoo-db", default="odoo_o2c")
    p.add_argument("--odoo-user", default="admin")
    p.add_argument("--odoo-password")
    args = p.parse_args()

    # 命令行 > .env / 环境变量。凭据建议只走后者，别进 shell 历史。
    _load_env_file()
    args.erp = args.erp or os.environ.get("ERP_URL")
    args.erp_company = args.erp_company or os.environ.get("ERP_COMPANY")
    args.erp_warehouse = args.erp_warehouse or os.environ.get("ERP_WAREHOUSE")
    if not args.erp_key and os.environ.get("ERP_API_KEY"):
        args.erp_key = f"{os.environ['ERP_API_KEY']}:{os.environ.get('ERP_API_SECRET', '')}"
    args.odoo = args.odoo or os.environ.get("ODOO_URL")
    args.odoo_db = args.odoo_db or os.environ.get("ODOO_DB")
    args.odoo_user = args.odoo_user or os.environ.get("ODOO_USER")
    args.odoo_password = args.odoo_password or os.environ.get("ODOO_PASSWORD")

    over = {"seed": args.seed, "concurrency": args.concurrency}
    for field, val in (
        ("orders", args.orders), ("online_orders", args.online_orders),
        ("customers", args.customers), ("skus", args.skus),
        ("erp_company", args.erp_company), ("erp_warehouse", args.erp_warehouse),
    ):
        if val:
            over[field] = val
    # 缩规模冒烟时，脏案例条数必须同比例缩，否则「800 张部分发货」在 40 张单里排不下
    if args.orders and args.orders < DEFAULT.orders:
        ratio = args.orders / DEFAULT.orders
        for field in (
            "shared_customers", "shared_exact", "shared_variant", "shared_attr_only",
            "odoo_only_customers", "orders_missing_in_erp", "foreign_currency_orders",
            "partial_shipment", "over_under_shipment", "returns", "partial_or_merged_payment",
            "bad_debt", "cross_period", "amended", "cancelled", "dirty_names",
            "rounding_residue", "stockout_delayed",
        ):
            over[field] = max(1, int(getattr(DEFAULT, field) * ratio))
        over["shared_customers"] = (
            over["shared_exact"] + over["shared_variant"] + over["shared_attr_only"]
        )
        over.setdefault("customers", max(over["shared_customers"] + 5, int(DEFAULT.customers * ratio)))
        over["spus"] = max(2, int(DEFAULT.spus * ratio))
        # 保底 8 个 SKU：订单行数最多 5，SKU 太少会让每张单都撞上同样几个商品，
        # 「SKU 销量 Top10」这类题失去意义
        over.setdefault("skus", max(8, over["spus"], int(DEFAULT.skus * ratio)))
        over["skus"] = max(over["skus"], over["spus"])  # 建表约束：SKU 不能少于 SPU
        over["online_orders"] = args.online_orders or max(1, int(DEFAULT.online_orders * ratio))
        over["orders_missing_in_erp"] = min(over["orders_missing_in_erp"], over["online_orders"])

    cfg: Recipe = replace(DEFAULT, **over)

    _log(f"生成台账 seed={cfg.seed} orders={cfg.orders} online={cfg.online_orders}")
    t0 = time.time()
    led = build_ledger(cfg)
    out = dump_truth(led, args.out)
    _log(f"台账完成 {time.time() - t0:.1f}s，真值已落 {out}")
    _log(
        f"  客户 {len(led.customers)} / SKU {len(led.products)} / 订单 {len(led.orders)}"
        f" / 发货 {sum(len(o.deliveries) for o in led.orders)}"
        f" / 发票 {len(led.invoices)} / 回款 {len(led.payments)}"
    )

    if args.only == "ledger":
        return 0

    failures: list[tuple[str, str]] = []

    if args.only in ("erp", "all"):
        if not (args.erp and args.erp_key and ":" in args.erp_key):
            p.error("投递 ERPNext 需要 --erp 与 --erp-key <key>:<secret>")
        from generator.deliver_erpnext import ErpClient, ErpnextDeliverer

        key, secret = args.erp_key.split(":", 1)
        dev = ErpnextDeliverer(ErpClient(args.erp, key, secret), cfg, _log)
        _log("=== ERPNext 主数据 ===")
        dev.ensure_masters(led)
        _log("=== ERPNext 单据（按日期时间轴推进，避免库存回溯重算风暴）===")
        t = time.time()
        dev.deliver(led)
        _log(f"ERPNext 完成 {(time.time() - t) / 60:.1f} 分钟，失败 {len(dev.failures)}")
        failures += dev.failures

    if args.only in ("odoo", "all"):
        if not (args.odoo and args.odoo_password):
            p.error("投递 Odoo 需要 --odoo 与 --odoo-password")
        from generator.deliver_odoo import OdooClient, OdooDeliverer

        dev = OdooDeliverer(
            OdooClient(args.odoo, args.odoo_db, args.odoo_user, args.odoo_password), cfg, _log
        )
        _log("=== Odoo 主数据 ===")
        dev.ensure_masters(led)
        _log("=== Odoo 单据（只投线上单）===")
        t = time.time()
        dev.deliver(led)
        _log(f"Odoo 完成 {(time.time() - t) / 60:.1f} 分钟，失败 {len(dev.failures)}")
        failures += dev.failures

    # 投递后回填的两侧单号写回真值，供一致性校验按单对账
    dump_truth(led, args.out)

    if failures:
        _log(f"!! 共 {len(failures)} 处失败，前 20 条：")
        for label, err in failures[:20]:
            _log(f"   [{label}] {err}")
        _log("修生成器后从 baseline 快照恢复重跑，不要手工补数据——")
        _log("手工补的行不进 truth.json，真值一旦和实际脱节，所有交叉校验就失效了。")
        return 1

    _log("全部投递成功。下一步：跑 README 步骤 4 的六项一致性校验，过了再打数据快照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
