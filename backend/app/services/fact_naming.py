"""事实/命名信号：表名或表含义为**动作/事件动词**（xx调整、xx交易、xx结算）或带**结构性
事实/明细特征**（xx明细、xx事实表、xx记录、xx行项）时，这张表记录的是一次业务**事实**或某
父表的**明细行**，而非一个业务**实体**——真正的业务对象是它引用的键（下单人、商品、账户…）。
据此把这类表识别为「事实关系」（本项目复用 bridge 角色）。

为什么要看表名（与 object_classifier「不依赖表名」的默认原则相对）：
    结构化事实/桥接判定（pk_all_fk、facet-B distinct_fk_targets）依赖 PK/FK 元数据，
    但真实 ERP 源常常**零 PK/FK/注释**，这些结构信号全部失效。此时表名/含义里的
    动作动词或明细/事实/记录特征往往是唯一能把「一次交易/一行明细」和「一个客户」区分开的
    线索。因此这里把命名作为一个**独立语义信号**引入，但仍以「加权打分 + needs_review」的方式
    参与，交人工复核，而非硬判定——命名脏时由复核闭环兜底。

匹配策略：
    - 中文动作动词、结构性事实/明细特征词按子串包含匹配（无词边界）；
    - 拉丁字母（英文/拼音缩写）按**词元边界**匹配，避免 login/catalog 里的 "log"、
      exchange 里的 "change" 这类误伤；
    - 结构性词表刻意排除易误伤词（裸「行」→银行/行业、「清单/排行/计划/单」→参考/派生/实体）。

词表按业务领域可调：真实源命名形态（中文 / 拼音 / 英文缩写）明确后，应据此增删。
"""

from __future__ import annotations

import re

# 中文事件/动作词元：命中即表明该表在记录「一次动作」而非「一个实体」。
# 覆盖财务过账、库存移动、审批流转、收付/退换、登记流水等常见业务事实。
_CJK_FACT_TOKENS: tuple[str, ...] = (
    "交易", "调整", "结算", "变更", "变动", "审批", "审核", "核销", "对账",
    "过账", "冲销", "冲红", "记账", "分录", "计提", "分摊", "分配", "调拨",
    "出库", "入库", "出入库", "盘点", "移库", "领用", "退库",
    "收款", "付款", "退款", "退货", "换货", "发货", "收货", "下单", "开票",
    "转账", "汇款", "报销", "报账", "登记", "流水", "打卡", "考勤",
    "提交", "撤销", "作废", "派工", "报工", "质检", "送检", "操作记录",
)

# 拉丁字母事件/动作词元（英文或拼音缩写）：按词元边界匹配。保守取强信号词，
# 避免与实体名撞车（如 order 既可为实体也可为动作，不纳入）。
_LATIN_FACT_TOKENS: frozenset[str] = frozenset(
    {
        "transaction", "txn", "trans", "adjustment", "adjust", "adj",
        "settlement", "settle", "journal", "voucher", "posting", "post",
        "refund", "payment", "receipt", "transfer", "changelog",
        "audit", "approval", "approve", "writeoff", "reconcile", "reconciliation",
        "movement", "issue", "receipt", "checkin", "checkout",
    }
)

# 结构性事实/明细特征词（CJK 子串匹配）：命名本身标明这是一次业务**事实**或某父表的
# **明细行**，而非独立实体——「订单明细」是订单的行、「订单事实表」是事实、「支付记录」
# 记录一次支付。与动作动词并列参与判定，真业务对象仍是它引用的键。
#
# 刻意排除易误伤词：不收裸「行」（会命中 银行/行业/配送行程/质量行动），改用「行项」「订单行」
# 这类复合词精确覆盖 财务报表行项/采购订单行；不收「清单/排行/计划/单」（价格清单/节假日清单
# 是参考数据、爆品排行是派生排名、生产计划是计划、订单是实体），避免把参考/计划/实体误判为事实。
_CJK_STRUCTURAL_FACT_TOKENS: tuple[str, ...] = (
    "明细", "事实", "记录", "行项", "订单行", "台账",
)


def _to_tokens(name: str) -> set[str]:
    """按非字母数字分隔拆词元（已 lower），用于拉丁词的词边界匹配。"""
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def detect_fact_name(name: str | None, meaning: str | None = None) -> str | None:
    """从技术表名 + 业务含义文本中识别事件/动作动词或结构性事实/明细特征，命中返回该词元，否则 None。

    参数
    ----
    name: 技术表名（如 stock_adjustment、gl_transaction、库存调整、订单明细）。
    meaning: 业务含义文本——display_name / description / 已挂术语拼接（如「库存调整单」「订单明细」）。

    两类信号都表明「这是一次业务事实/明细」而非独立实体：
    - **动作动词**（交易/调整/transaction…）：记录一次动作；
    - **结构性特征**（明细/事实/记录/行项…）：某父表的明细行或一条事实流水。

    命中优先返回中文词元（可读性更好），其次拉丁词元。
    """
    hay_cjk = f"{name or ''}{meaning or ''}"
    for token in _CJK_FACT_TOKENS:
        if token in hay_cjk:
            return token
    for token in _CJK_STRUCTURAL_FACT_TOKENS:
        if token in hay_cjk:
            return token

    tokens = _to_tokens(name or "") | _to_tokens(meaning or "")
    for token in _LATIN_FACT_TOKENS:
        if token in tokens:
            return token

    return None
