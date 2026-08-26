"""Dialect Adapter 抽象基类。

引擎特定逻辑**只允许**存在于本包的各 Adapter 实现中。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from app.warehouse.capabilities import (
    Capabilities,
    CapabilityError,
    CapabilityGap,
    check_table,
)
from app.warehouse.logical_schema import LogicalTable


def base_type(data_type: str | None) -> str:
    """物理类型去掉参数与修饰，只留基名。``VARCHAR(140)`` → ``varchar``。

    真实源采回来的是带参数的原样类型（``INTEGER(11)``、``DECIMAL(21, 9)``、
    ``DATETIME(6)``）。各 Adapter 的 map_type 用精确匹配查表，这些一个都命中不了，
    于是**整数列、金额列全被判成文本**落进数仓——数值语义就此丢失，下游聚合要么报错
    要么按字符串比大小。日期类靠子串判断侥幸躲过，数值类没这个运气。
    """
    lowered = (data_type or "").strip().lower()
    return re.split(r"[(\s]", lowered, maxsplit=1)[0] if lowered else ""


class DialectAdapter(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """本引擎能表达什么。未核实的条目须置 ``verified=False``。"""

    @abstractmethod
    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        """本体类型 → 引擎原生类型。"""

    @abstractmethod
    def render_create_table(self, table: LogicalTable) -> str:
        """渲染建表语句。实现须先调用 ``self.guard(table)``。"""

    @abstractmethod
    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        """本体变更 → ALTER 语句；无法用 ALTER 表达时返回重建指引。"""

    def render_foreign_keys(self, table: LogicalTable) -> list[str]:
        """**建表之后**再补的外键语句。默认空 = 该引擎把外键写进建表语句里。

        真正强制外键的引擎（postgres）必须走这条路：``CREATE TABLE`` 内联 REFERENCES 时，
        被引用表还不存在就直接报错，于是「只物化本体的一部分」「本体里存在环」这两件事都
        做不了——而 ERP 这类真实本体的血缘本来就是环。拆成两段（先全部建表、再统一加约束）
        才与建表顺序无关。

        声明式外键的引擎（hive 写进 TBLPROPERTIES、doris 之流不支持）返回空即可：
        它们的外键随建表一起落，本就没有顺序问题。
        """
        return []

    def translate_sql(self, sql: str) -> str:
        """SQL 方言翻译。默认原样返回。"""
        return sql

    def render_load(self, target: str, select_body: str, *, overwrite: bool) -> str:
        """装载语句：把一段 SELECT 的结果写进目标表。``overwrite`` 为覆盖装载。

        默认是 Hive 家族的 ``INSERT OVERWRITE TABLE`` / ``INSERT INTO TABLE``。
        **标准 SQL 引擎没有这两种写法**（postgres 见到 ``INSERT OVERWRITE`` 直接语法
        报错，``INSERT INTO TABLE x`` 里的 TABLE 也是多余的），必须覆写——此前生成器
        把这句写死成 Hive 写法，选了 postgres 目标仓时产出的是一条跑不了的 SQL。

        返回单个字符串（可含多条语句，如 postgres 的 ``TRUNCATE`` + ``INSERT``）。
        """
        verb = "INSERT OVERWRITE TABLE" if overwrite else "INSERT INTO TABLE"
        return f"{verb} {target}\n{select_body};"

    def quote_identifier(self, name: str) -> str:
        """引擎标识符引用规则。默认反引号；ANSI 双引号引擎须覆写。

        引号规则是引擎特定逻辑，必须住在 Adapter 里——生成器只能委托本方法，
        绝不允许自己判 ``if engine == ...``（遗留2 下沉的正是此处）。
        """
        return f"`{name}`"

    def quote_table_ref(self, ref: str) -> str:
        """``库.表`` 这类点分表引用**逐段**加引号。

        源表名来自 DataHub URN，是真实源里的原名——Frappe/ERPNext 的
        ``tabAccount Category``、``tabAsset Repair Purchase Invoice`` 带空格，
        不引用产出的就是一条任何引擎都拒绝解析的 SQL。这类名字在 ERP 域里占比很高，
        不是边角情况，故生成器一律走本方法而非直接拼字符串。

        按 ``.`` 分段（``库.表``、``目录.库.表`` 均可）。段内含 ``.`` 的表名不在
        本方法的表达范围内——那需要调用方自己拆好再逐段引用。
        """
        parts = [p for p in (ref or "").split(".") if p]
        return ".".join(self.quote_identifier(p) for p in parts)

    # ---------- 全量落地：staging + 原子切换（M15） ----------
    #
    # 现状的全量装载是先删后写（SeaTunnel DROP_DATA），搬到一半失败=正式表被清空、
    # 原数据没了（§1.2 最贵的一种不稳定）。M15 改成：搬进一张 staging 表 → 校验 →
    # **原子切换**到正式表，失败时正式表原封不动。切换语法各引擎不同，故落在 Adapter
    # 里（与建表 DDL 同处），**不进 runner**（runner 只把数据写进 staging）。
    #
    # ⚠ 需实施前验证（MATERIALIZE_SYNC_STABILITY.md §8.3）：各引擎切换的原子性与代价
    # 需在真实实例上核实——本层只保证生成的语句形状正确、可 golden，不代表已在目标库跑通。

    def _sanitize_run(self, run_id: str) -> str:
        """run_id → 可作表名后缀的 token。Airflow run_id 常含冒号/加号/减号，须清洗。"""
        return re.sub(r"[^0-9A-Za-z]+", "_", run_id or "").strip("_") or "run"

    def staging_table_name(self, table: LogicalTable, run_id: str) -> str:
        """staging 表裸名 ``<表>__stg_<run>``。带 run_id：重跑/补数不撞表（§3.3 幂等）。"""
        return f"{table.name}__stg_{self._sanitize_run(run_id)}"

    def _qual(self, database: str | None, name: str) -> str:
        """``库.表`` 或 ``表``，按本引擎的引用规则加引号。"""
        q = self.quote_identifier
        return f"{q(database)}.{q(name)}" if database else q(name)

    def render_create_staging(self, table: LogicalTable, run_id: str) -> str:
        """建一张与正式表同构的空 staging 表。默认 ``CREATE TABLE ... LIKE``。"""
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        return f"CREATE TABLE IF NOT EXISTS {stg} LIKE {orig};"

    def render_truncate_staging(self, table: LogicalTable, run_id: str) -> str:
        """清空 staging。**全量装载必须从空表开始**。

        staging 名按批次而非按运行（见 ``_plan_staging``），所以它会跨运行复用：
        上一次运行如果搬完但切换失败，那批行就留在 staging 里，下一次全量再
        ``INSERT INTO`` 一遍——切换过去的正式表于是每行都有两份。源 5 行、目标 10 行，
        而两个作业各自都报成功。``CREATE TABLE IF NOT EXISTS`` 对这种残留是沉默的，
        所以清空要显式做。
        """
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        return f"TRUNCATE TABLE {stg};"

    def render_swap(self, table: LogicalTable, run_id: str) -> list[str]:
        """把 staging 原子切换成正式表。

        默认实现是 **rename 两步**（正式→old，staging→正式，再删 old）：正式表被改名走后、
        staging 改名前有一个**短暂窗口**，不是真正原子——这是没有原生原子切换的引擎的下限。
        有 ``REPLACE`` / ``SWAP`` / ``EXCHANGE`` 的引擎覆写本方法换成单语句原子操作。
        """
        run = self._sanitize_run(run_id)
        stg = self._qual(table.database, self.staging_table_name(table, run_id))
        orig = self._qual(table.database, table.name)
        old = self._qual(table.database, f"{table.name}__old_{run}")
        return [
            f"DROP TABLE IF EXISTS {old};",
            f"ALTER TABLE {orig} RENAME TO {old};",
            f"ALTER TABLE {stg} RENAME TO {orig};",
            f"DROP TABLE IF EXISTS {old};",
        ]

    # ---------- 目标实例的实测事实 ----------

    def for_storage_nodes(self, count: int | None) -> DialectAdapter:
        """按目标实例**实测的存储节点数**定制一个渲染器；默认返回自身。

        建表 DDL 里有一类值不是本体决定的，也不是引擎方言决定的，而是**这套实例有多大**
        决定的——最典型的就是副本数。Doris 建表不写 ``replication_num`` 时取 FE 的
        ``default_replication_num``（默认 3），单 BE 的实例上每一条建表语句都会被拒：

            errCode = 2, detailMessage = replication num should be less than the number
            of available backends. replication num is 3, available backend num is 1

        节点数由调用方在目标仓上实测后传进来（读不到就传 None，此时行为逐字节不变——
        宁可沿用引擎默认，也不按猜的数字建表）。**换算成什么 DDL 是引擎知识**，故留在
        各 Adapter 里，生成器只管把实测数字递过来。
        """
        return self

    # ---------- 能力校验 ----------

    def check(self, table: LogicalTable) -> list[CapabilityGap]:
        """返回全部缺口（含 warning）。Validation Gate 调这个做前置校验。"""
        return check_table(table, self.capabilities())

    def guard(self, table: LogicalTable) -> list[CapabilityGap]:
        """error 级缺口直接抛出；warning 级返回给调用方呈现，**不得吞掉**。"""
        gaps = self.check(table)
        errors = [g for g in gaps if g.is_error]
        if errors:
            raise CapabilityError(self.name, errors)
        return gaps


class UnimplementedAdapter(DialectAdapter):
    """只声明能力、尚未实现渲染的占位 Adapter（M8 补齐）。

    刻意保留 ``capabilities()``：即便还不能渲染，Validation Gate 也能提前告诉用户
    「这个引擎表达不了你要的东西」，而不是等到实现完才发现。
    """

    def map_type(self, data_type: str | None, semantic_type: str | None) -> str:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")

    def render_create_table(self, table: LogicalTable) -> str:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")

    def render_alter(self, before: LogicalTable, after: LogicalTable) -> list[str]:
        raise NotImplementedError(f"{self.name} adapter 尚未实现（M8）")
