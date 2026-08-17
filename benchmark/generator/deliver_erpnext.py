"""ERPNext 投递器：把中立台账翻译成 ERPNext 单据。

**按单据日期的时间轴推进，不是按订单推进。** ERPNext 对回溯的库存单据会排
``Repost Item Valuation``，重算该物料该仓库此后的全部流水。若逐张订单跑完整链条
（2 月的单先发到 2 月 20 日，再回头处理 2 月 2 日的单发到 2 月 5 日），后者就是回溯，
会触发成千上万次重算，队列积压到跑一整天也做不完——而且是在你以为跑完之后才显现。

所以：把所有单据摊成 (日期, 阶段) 的事件流，按日期升序、日内按 SO→DN→SI→PE 分阶段推进，
每个阶段内并发。日内分阶段是必要的：发票可能与发货同日，不分阶段会抢在发货前建。

并发度取 gunicorn worker 数——提交是同步执行在 web worker 里的，开更大只会排队。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable

import requests

from .config import Recipe
from .ledger import Ledger, Order

STAGE_SO, STAGE_DN, STAGE_SI, STAGE_PE = "SO", "DN", "SI", "PE"
STAGES = (STAGE_SO, STAGE_DN, STAGE_SI, STAGE_PE)


class ErpError(RuntimeError):
    pass


class ErpClient:
    """ERPNext REST 薄封装。鉴权用 API Key/Secret（Administrator 生成）。"""

    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._local = threading.local()
        self._auth = f"token {api_key}:{api_secret}"

    @property
    def session(self) -> requests.Session:
        # 每线程一个 Session：requests.Session 不保证线程安全
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Authorization": self._auth, "Accept": "application/json"})
            self._local.s = s
        return s

    def _check(self, resp: requests.Response, what: str) -> Any:
        if resp.status_code >= 400:
            # ERPNext 把真正的原因埋在 _server_messages / exception 里，
            # 直接抛 HTTP 状态码等于没说
            detail = resp.text[:1200]
            raise ErpError(f"{what} 失败 HTTP {resp.status_code}: {detail}")
        return resp.json()

    def _request(self, what: str, fn: Callable[[], "requests.Response"]) -> Any:
        """带重试的请求。

        ``tabSeries``（单据编号表）在 8 并发提交下会抛两类瞬态错误——
        ``QueryDeadlockError``（编号 UPDATE 撞乐观锁）与 ``Duplicate entry``
        （系列首次 INSERT 撞车）。事务已回滚，重试是安全且幂等的（不会造出重复单据）。
        """
        import time

        last: Exception | None = None
        for attempt in range(10):
            try:
                return self._check(fn(), what)
            except ErpError as exc:
                msg = str(exc)
                if (
                    "QueryDeadlockError" in msg
                    or "Record has changed" in msg
                    or "Duplicate entry" in msg
                    or "HTTP 5" in msg
                ):
                    last = exc
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        assert last is not None
        raise last

    def insert(self, doctype: str, doc: dict) -> dict:
        return self._request(
            f"新建 {doctype}",
            lambda: self.session.post(
                f"{self.base}/api/resource/{doctype}", json=doc, timeout=self.timeout
            ),
        )["data"]

    def submit(self, doctype: str, name: str) -> dict:
        return self._request(
            f"提交 {doctype} {name}",
            lambda: self.session.put(
                f"{self.base}/api/resource/{doctype}/{name}", json={"docstatus": 1}, timeout=self.timeout
            ),
        )["data"]

    def cancel(self, doctype: str, name: str) -> dict:
        return self._request(
            f"取消 {doctype} {name}",
            lambda: self.session.put(
                f"{self.base}/api/resource/{doctype}/{name}", json={"docstatus": 2}, timeout=self.timeout
            ),
        )["data"]

    def call(self, method: str, payload: dict) -> Any:
        return self._request(
            f"调用 {method}",
            lambda: self.session.post(
                f"{self.base}/api/method/{method}", json=payload, timeout=self.timeout
            ),
        ).get("message")

    def get_value(self, doctype: str, filters: dict, field: str) -> Any:
        msg = self._request(
            f"取 {doctype}.{field}",
            lambda: self.session.get(
                f"{self.base}/api/method/frappe.client.get_value",
                params={"doctype": doctype, "filters": _json(filters), "fieldname": field},
                timeout=self.timeout,
            ),
        ).get("message") or {}
        return msg.get(field)


def _json(v: Any) -> str:
    import json

    return json.dumps(v, ensure_ascii=False)


def _d(x: Decimal) -> float:
    return float(x)


class ErpnextDeliverer:
    def __init__(self, client: ErpClient, cfg: Recipe, log: Callable[[str], None] = print):
        self.c = client
        self.cfg = cfg
        self.log = log
        self._lock = threading.Lock()
        self.erp_name: dict[str, str] = {}  # 台账键 -> ERPNext 单号
        self.failures: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- 主数据

    def ensure_masters(self, led: Ledger) -> None:
        cfg = self.cfg
        company = cfg.erp_company or self.c.get_value("Company", {}, "name")
        warehouse = cfg.erp_warehouse or self.c.get_value(
            "Warehouse", {"is_group": 0, "company": company}, "name"
        )
        if not company or not warehouse:
            raise ErpError("取不到默认公司/仓库——Setup Wizard 是不是还没走完？")
        self.company, self.warehouse = company, warehouse
        self.log(f"公司={company} 仓库={warehouse}")

        customers = [c for c in led.customers if c.erp_name]
        self._parallel(
            customers,
            lambda c: self._put(
                c.key,
                "Customer",
                {
                    "customer_name": c.erp_name,
                    "customer_group": cfg.erp_customer_group,
                    "territory": cfg.erp_territory,
                    "tax_id": c.tax_id,
                    "mobile_no": c.phone,
                },
            ),
            "客户",
        )
        self._parallel(
            led.products,
            lambda p: self._put(
                p.key,
                "Item",
                {
                    "item_code": p.key,
                    "item_name": f"{p.name} {p.spec}",
                    "item_group": cfg.erp_item_group,
                    "stock_uom": cfg.erp_uom,
                    "is_stock_item": 1,
                    "valuation_rate": _d(p.std_cost),
                    # over_under 脏案例会超发/短发（factor 1.1/1.2）+ partial 拆批四舍五入
                    # 溢出 0.01。放开 item 级超量允许量——全局 Stock Settings 走的是
                    # frappe 缓存，直接 SQL 改不动，建 item 时带上最稳。
                    "over_delivery_receipt_allowance": 100,
                    "over_billing_allowance": 100,
                },
            ),
            "物料",
        )
        self._opening_stock(led)

    def _opening_stock(self, led: Ledger) -> None:
        """期初入库：没有库存就发不出货。分批提交，一张单塞 800 行会超时。

        日期放在窗口开始**前一天**——它必须早于所有出库单，否则每张出库单都成了回溯。
        """
        posting = self.cfg.start.fromordinal(self.cfg.start.toordinal() - 1)
        batch = 200
        items = led.products
        for i in range(0, len(items), batch):
            chunk = items[i : i + batch]
            doc = {
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Receipt",
                "company": self.company,
                "posting_date": posting.isoformat(),
                "set_posting_time": 1,
                "items": [
                    {
                        "item_code": p.key,
                        "qty": 100000,
                        "t_warehouse": self.warehouse,
                        "basic_rate": _d(p.std_cost),
                    }
                    for p in chunk
                ],
            }
            created = self.c.insert("Stock Entry", doc)
            self.c.submit("Stock Entry", created["name"])
            self.log(f"期初入库 {i + len(chunk)}/{len(items)}")

    # ---------------------------------------------------------------- 时间轴

    def deliver(self, led: Ledger) -> None:
        timeline: dict[date, dict[str, list]] = defaultdict(lambda: {s: [] for s in STAGES})
        for o in led.orders:
            if not o.in_erp:
                continue  # 未下传的线上单 —— 跨系统题的答案，故意不建
            timeline[o.ordered_on][STAGE_SO].append(o)
            for d in o.deliveries:
                timeline[d.delivered_on][STAGE_DN].append((o, d))
            for inv in o.invoices:
                timeline[inv.posted_on][STAGE_SI].append((o, inv))
        for p in led.payments:
            timeline[p.paid_on][STAGE_PE].append(p)

        handlers = {
            STAGE_SO: self._do_order,
            STAGE_DN: lambda x: self._do_delivery(*x),
            STAGE_SI: lambda x: self._do_invoice(*x),
            STAGE_PE: self._do_payment,
        }
        for i, day in enumerate(sorted(timeline), start=1):
            for stage in STAGES:
                work = timeline[day][stage]
                if work:
                    self._parallel(work, handlers[stage], f"{day} {stage}", quiet=True)
            if i % 10 == 0:
                self.log(f"时间轴推进到 {day}（第 {i} 天），失败 {len(self.failures)}")

    # ---------------------------------------------------------------- 单据

    def _do_order(self, o: Order) -> None:
        cust = self._need(o.customer_key)
        doc = {
            "doctype": "Sales Order",
            "customer": cust,
            "company": self.company,
            "transaction_date": o.ordered_on.isoformat(),
            "delivery_date": o.promised_on.isoformat(),
            # 外币单（foreign_currency_orders）在 ERPNext 侧按 CNY 落——ERPNext 客户默认
            # 币种是 CNY，外币单需要给客户配 USD 应收账户（本环境未配），否则 SI 阶段会触发
            # 「结算货币需与业务交易货币相同」。台账仍记 USD（真值），投递时按 CNY，金额数值不变。
            "currency": "CNY",
            "po_no": o.po_no or "",
            "items": [
                {
                    "item_code": ln.sku,
                    "qty": _d(ln.qty),
                    "rate": _d(ln.unit_price),
                    "delivery_date": o.promised_on.isoformat(),
                    "warehouse": self.warehouse,
                }
                for ln in o.lines
            ],
        }
        if o.rounding_residue:
            # 聚合口径陷阱：表头比行合计少 0.01。ERPNext 的表头由行算出，改不了，
            # 只能挂一笔整单折扣把差额做出来——否则这条脏案例只存在于 truth.json 里。
            doc["apply_discount_on"] = "Grand Total"
            doc["discount_amount"] = 0.01
        created = self.c.insert("Sales Order", doc)
        name = created["name"]
        self.c.submit("Sales Order", name)

        if o.amended:
            # cancel + amend，产生 amended_from 链。必须在发货之前做——
            # 已有交货单的订单取消不了。
            self.c.cancel("Sales Order", name)
            doc["amended_from"] = name
            created = self.c.insert("Sales Order", doc)
            name = created["name"]
            self.c.submit("Sales Order", name)

        if o.status == "cancelled":
            self.c.cancel("Sales Order", name)

        with self._lock:
            self.erp_name[o.key] = name
        o.erp_name = name

    def _do_delivery(self, o: Order, d) -> None:
        so = self._need(o.key)
        if d.is_return:
            self._do_return(o, d, so)
            return
        # 必须走标准转换函数：它会带出 against_sales_order / so_detail 等引用字段，
        # 履约周期、按时交付率、订单到发票的血缘全靠它们。手工拼的链条缺引用，
        # 指标会静默算错。
        doc = self.c.call(
            "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
            {"source_name": so},
        )
        qty_of = {ln.sku: ln.qty for ln in d.lines}
        doc["posting_date"] = d.delivered_on.isoformat()
        doc["set_posting_time"] = 1
        kept = []
        for row in doc.get("items", []):
            q = qty_of.get(row.get("item_code"))
            if q is None:
                continue
            row["qty"] = _d(q)
            row["warehouse"] = self.warehouse
            kept.append(row)
        doc["items"] = kept
        created = self.c.insert("Delivery Note", doc)
        self.c.submit("Delivery Note", created["name"])
        with self._lock:
            self.erp_name[d.key] = created["name"]

    def _do_return(self, o: Order, d, so: str) -> None:
        src = self.erp_name.get(o.deliveries[0].key)
        if not src:
            raise ErpError(f"{o.key} 的退货找不到原交货单")
        doc = {
            "doctype": "Delivery Note",
            "customer": self._need(o.customer_key),
            "company": self.company,
            "is_return": 1,
            "return_against": src,
            "posting_date": d.delivered_on.isoformat(),
            "set_posting_time": 1,
            "items": [
                {
                    "item_code": ln.sku,
                    "qty": _d(ln.qty),  # 已是负数
                    "rate": _d(ln.unit_price),
                    "warehouse": self.warehouse,
                }
                for ln in d.lines
            ],
        }
        created = self.c.insert("Delivery Note", doc)
        self.c.submit("Delivery Note", created["name"])
        with self._lock:
            self.erp_name[d.key] = created["name"]

    def _do_invoice(self, o: Order, inv) -> None:
        dn = self.erp_name.get(inv.delivery_key or "")
        if not dn:
            raise ErpError(f"发票 {inv.key} 找不到对应交货单")
        doc = self.c.call(
            "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice",
            {"source_name": dn},
        )
        doc["posting_date"] = inv.posted_on.isoformat()
        doc["set_posting_time"] = 1
        doc["due_date"] = inv.posted_on.isoformat()
        created = self.c.insert("Sales Invoice", doc)
        self.c.submit("Sales Invoice", created["name"])
        with self._lock:
            self.erp_name[inv.key] = created["name"]

    def _do_payment(self, p) -> None:
        names = [self.erp_name.get(k) for k in p.invoice_keys]
        names = [n for n in names if n]
        if not names:
            return  # 对应发票没建成（多半是上游失败），跳过并留给一致性校验发现
        doc = self.c.call(
            "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
            {"dt": "Sales Invoice", "dn": names[0]},
        )
        doc["posting_date"] = p.paid_on.isoformat()
        doc["reference_date"] = p.paid_on.isoformat()
        doc["paid_amount"] = _d(p.amount)
        doc["received_amount"] = _d(p.amount)
        refs = doc.get("references") or []
        if len(names) > 1:
            # 合并回款：一笔覆盖多张发票
            extra = []
            for n in names[1:]:
                amt = self.c.get_value("Sales Invoice", {"name": n}, "outstanding_amount") or 0
                extra.append(
                    {"reference_doctype": "Sales Invoice", "reference_name": n, "allocated_amount": amt}
                )
            refs = refs + extra
        elif refs:
            # 部分回款：分配额小于应收，账龄里留尾巴。但退货的贷项会冲减应收，
            # 全款回款时 p.amount 可能大于实际 outstanding——用 min 兜住，别让分配额超应收。
            refs[0]["allocated_amount"] = min(
                _d(p.amount), float(refs[0].get("allocated_amount") or p.amount)
            )
        doc["references"] = refs
        created = self.c.insert("Payment Entry", doc)
        self.c.submit("Payment Entry", created["name"])

    # ---------------------------------------------------------------- 工具

    def _put(self, key: str, doctype: str, doc: dict) -> None:
        created = self.c.insert(doctype, doc)
        with self._lock:
            self.erp_name[key] = created["name"]

    def _need(self, key: str) -> str:
        name = self.erp_name.get(key)
        if not name:
            raise ErpError(f"台账键 {key} 尚未在 ERPNext 建出来（上游是不是失败了？）")
        return name

    def _parallel(
        self, items: Iterable, fn: Callable, label: str, quiet: bool = False
    ) -> None:
        items = list(items)
        if not items:
            return
        errs: list[tuple[str, str]] = []

        def run(x):
            try:
                fn(x)
            except Exception as exc:  # noqa: BLE001 — 单据失败不该中断整轮，末尾统一报
                errs.append((label, str(exc)[:300]))
                self.log(f"  !! 失败 {label}: {str(exc)[:200]}")

        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            list(pool.map(run, items))
        if errs:
            with self._lock:
                self.failures.extend(errs)
        if not quiet:
            self.log(f"{label}: {len(items) - len(errs)}/{len(items)} 成功")
