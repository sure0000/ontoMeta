"""Odoo 投递器：把台账里的**线上单**翻译成 Odoo 单据（它扮演前端电商系统）。

只投 online 那部分（默认 1800 张），不是全部 6000 张——这个不对称本身就是跨系统题的题面：
「线上下单但 ERP 里没有的有多少」「同一客户在两系统的应收合计」。

三处刻意制造的差异（真值在 truth.json，不是从系统里反推）：
- 客户名：exact / variant / attr_only 三档，难匹配那档只有税号或电话能对上
- 商品：这边是 product.template(SPU) + product.product(SKU) 两层，ERP 只有 Item 一层
- 状态机：Odoo 的 draft/sent/sale/done/cancel 与 ERP 的七态不是一一对应

XML-RPC 的坑：``execute_kw`` 调不了下划线开头的方法，所以开票不能用
``_create_invoices()``，得走 ``sale.advance.payment.inv`` 向导；``button_validate``
在部分发货时会返回向导动作而不是直接完成，要接住再 process。
"""

from __future__ import annotations

import threading
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Callable, Iterable

from .config import Recipe
from .ledger import Ledger, Order


class OdooError(RuntimeError):
    pass


class OdooClient:
    def __init__(self, url: str, db: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.db, self.user, self.pwd = db, username, password
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self.uid = common.authenticate(db, username, password, {})
        if not self.uid:
            raise OdooError(f"Odoo 认证失败：db={db} user={username}")
        self._local = threading.local()

    @property
    def models(self):
        m = getattr(self._local, "m", None)
        if m is None:
            m = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
            self._local.m = m
        return m

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        try:
            return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args, kwargs or {})
        except xmlrpc.client.Fault as fault:
            raise OdooError(f"{model}.{method} 失败: {str(fault.faultString)[:600]}") from fault

    def create(self, model: str, vals: dict, context: dict | None = None) -> int:
        return self.call(model, "create", [vals], {"context": context} if context else None)

    def write(self, model: str, ids: list[int], vals: dict) -> Any:
        return self.call(model, "write", [ids, vals])

    def search_read(self, model: str, domain: list, fields: list[str], limit: int = 0) -> list[dict]:
        kw: dict[str, Any] = {"fields": fields}
        if limit:
            kw["limit"] = limit
        return self.call(model, "search_read", [domain], kw)


def _f(x: Decimal) -> float:
    return float(x)


class OdooDeliverer:
    def __init__(self, client: OdooClient, cfg: Recipe, log: Callable[[str], None] = print):
        self.c = client
        self.cfg = cfg
        self.log = log
        self._lock = threading.Lock()
        self.partner: dict[str, int] = {}
        self.product: dict[str, int] = {}  # SKU -> product.product id
        self.template: dict[str, int] = {}  # SPU -> product.template id
        self.order_id: dict[str, int] = {}
        self.failures: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- 主数据

    def ensure_masters(self, led: Ledger) -> None:
        customers = [c for c in led.customers if c.odoo_name]
        self._parallel(
            customers,
            lambda c: self._set(
                self.partner,
                c.key,
                self.c.create(
                    "res.partner",
                    {"name": c.odoo_name, "vat": c.tax_id, "phone": c.phone, "company_type": "company"},
                ),
            ),
            "客户",
        )

        # SPU 建 template，SKU 建 product。粒度差就在这里：ERP 侧只有一层 Item。
        spus: dict[str, list] = {}
        for p in led.products:
            spus.setdefault(p.spu_key, []).append(p)

        def make_spu(item):
            spu_key, skus = item
            tmpl = self.c.create(
                "product.template",
                {"name": skus[0].name, "list_price": _f(skus[0].list_price), "type": "consu"},
            )
            self._set(self.template, spu_key, tmpl)
            for p in skus:
                pid = self.c.create(
                    "product.product",
                    {
                        "product_tmpl_id": tmpl,
                        "default_code": p.key,
                        "lst_price": _f(p.list_price),
                        "standard_price": _f(p.std_cost),
                    },
                )
                self._set(self.product, p.key, pid)

        self._parallel(list(spus.items()), make_spu, "商品(SPU/SKU)")

    # ---------------------------------------------------------------- 单据

    def deliver(self, led: Ledger) -> None:
        online = [o for o in led.orders if o.in_odoo]
        online.sort(key=lambda o: (o.ordered_on, o.key))
        # Odoo 不像 ERPNext 有库存重算风暴的问题，但仍按日期推进——
        # 保持两边时间轴一致，出问题时好对账
        by_day: dict[Any, list[Order]] = {}
        for o in online:
            by_day.setdefault(o.ordered_on, []).append(o)
        for i, day in enumerate(sorted(by_day), start=1):
            self._parallel(by_day[day], self._do_order, f"{day}", quiet=True)
            if i % 20 == 0:
                self.log(f"Odoo 推进到 {day}（第 {i} 天），失败 {len(self.failures)}")

    def _do_order(self, o: Order) -> None:
        partner = self.partner.get(o.customer_key)
        if not partner:
            raise OdooError(f"{o.customer_key} 在 Odoo 侧没有 partner")
        lines = [
            (0, 0, {
                "product_id": self.product[ln.sku],
                "product_uom_qty": _f(ln.qty),
                "price_unit": _f(ln.unit_price),
            })
            for ln in o.lines
        ]
        so = self.c.create(
            "sale.order",
            {
                "partner_id": partner,
                "date_order": f"{o.ordered_on.isoformat()} 08:00:00",
                "client_order_ref": o.po_no,  # 跨系统关联键，ERP 侧落在 po_no
                "order_line": lines,
            },
        )
        self._set(self.order_id, o.key, so)
        o.odoo_name = str(so)

        if o.status == "cancelled":
            # 状态机差异的来源：Odoo 只有 cancel 一态，ERP 侧区分 Cancelled / Closed
            self.c.call("sale.order", "action_cancel", [[so]])
            return

        self.c.call("sale.order", "action_confirm", [[so]])
        self._validate_pickings(so, o)
        self._invoice(so, o)

    def _validate_pickings(self, so: int, o: Order) -> None:
        pickings = self.c.search_read("stock.picking", [("sale_id", "=", so)], ["id", "state"])
        for pick in pickings:
            if pick["state"] in ("done", "cancel"):
                continue
            moves = self.c.search_read(
                "stock.move", [("picking_id", "=", pick["id"])], ["id", "product_uom_qty"]
            )
            for mv in moves:
                # Odoo 17 用 quantity；16 及以前是 quantity_done。写不进就退回旧字段名。
                try:
                    self.c.write("stock.move", [mv["id"]], {"quantity": mv["product_uom_qty"]})
                except OdooError:
                    self.c.write("stock.move", [mv["id"]], {"quantity_done": mv["product_uom_qty"]})
            res = self.c.call("stock.picking", "button_validate", [[pick["id"]]])
            self._settle_wizard(res)
            # 出库日期回填成台账的发货日，否则全落在跑数当天，履约周期无从算起
            first = o.first_delivery_on or o.ordered_on
            self.c.write("stock.picking", [pick["id"]], {"date_done": f"{first.isoformat()} 10:00:00"})

    def _settle_wizard(self, res: Any) -> None:
        """button_validate 在部分发货/立即调拨时返回向导动作而不是直接完成。
        不接住的话单据停在 assigned，后面开票拿不到可开票数量。"""
        if not isinstance(res, dict) or not res.get("res_model"):
            return
        model = res["res_model"]
        ctx = res.get("context") or {}
        wiz = self.c.create(model, {}, context=ctx)
        for method in ("process", "action_confirm"):
            try:
                self.c.call(model, method, [[wiz]])
                return
            except OdooError:
                continue

    def _invoice(self, so: int, o: Order) -> None:
        """开票必须走向导：execute_kw 调不了 _create_invoices（下划线开头）。"""
        ctx = {"active_model": "sale.order", "active_ids": [so], "active_id": so}
        wiz = self.c.create("sale.advance.payment.inv", {"advance_payment_method": "delivered"}, context=ctx)
        self.c.call("sale.advance.payment.inv", "create_invoices", [[wiz]], {"context": ctx})

        moves = self.c.search_read("account.move", [("invoice_origin", "!=", False), ("state", "=", "draft")], ["id", "invoice_origin"])
        so_name = self.c.search_read("sale.order", [("id", "=", so)], ["name"])
        name = so_name[0]["name"] if so_name else None
        mine = [m["id"] for m in moves if m.get("invoice_origin") == name]
        if not mine:
            return
        posted_on = (o.invoices[0].posted_on if o.invoices else o.ordered_on).isoformat()
        self.c.write("account.move", mine, {"invoice_date": posted_on, "date": posted_on})
        self.c.call("account.move", "action_post", [mine])

    # ---------------------------------------------------------------- 工具

    def _set(self, target: dict, key: str, value: int) -> None:
        with self._lock:
            target[key] = value

    def _parallel(self, items: Iterable, fn: Callable, label: str, quiet: bool = False) -> None:
        items = list(items)
        if not items:
            return
        errs: list[tuple[str, str]] = []

        def run(x):
            try:
                fn(x)
            except Exception as exc:  # noqa: BLE001
                errs.append((label, str(exc)[:300]))

        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            list(pool.map(run, items))
        if errs:
            with self._lock:
                self.failures.extend(errs)
        if not quiet:
            self.log(f"Odoo {label}: {len(items) - len(errs)}/{len(items)} 成功")
