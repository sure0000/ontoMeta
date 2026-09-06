"""只读校验闸：放行该放的，拦住该拦的。

这道闸是 Data Agent / MCP 代跑 SQL 的唯一写侧防线，所以两个方向都要钉：

- **别漏**：叠句、Postgres 的数据修改型 CTE、``COPY ... TO PROGRAM`` 这些绕过尝试；
- **别误伤**：原实现在整条小写化 SQL 上跑正则，字符串字面量与注释里的词也会命中——
  ``WHERE note = 'please delete this'`` 被判「包含禁止的关键字：delete」。
  ERP 的文本字段里出现 delete/update/create 是常态，误伤会让人以为是模型出错了。
"""

from __future__ import annotations

import pytest

from app.services.data_app_executor import is_read_only

ALLOWED = [
    ("SELECT 1", "最简查询"),
    ("select id, name from customer where level = 'vip'", "普通查询"),
    ("WITH t AS (SELECT 1 AS n) SELECT n FROM t", "只读 CTE"),
    ("SELECT count(*) FROM orders GROUP BY status", "聚合"),
    # 下面三条改判之前全部被误拒
    ("SELECT name FROM t WHERE note = 'please delete this'", "字面量里含 delete"),
    ("SELECT * FROM t WHERE memo = 'create a new one'", "字面量里含 create"),
    ("SELECT * FROM t -- create table x", "注释里含 create"),
    ("SELECT a FROM t /* insert 说明 */ WHERE a > 1", "块注释里含 insert"),
    ("SELECT remark FROM t WHERE remark LIKE '%update%'", "LIKE 模式里含 update"),
]

BLOCKED = [
    ("SELECT * FROM t; DROP TABLE t", "叠句"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "PG 数据修改型 CTE"),
    ("WITH u AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM u", "PG 数据修改型 CTE（update）"),
    ("COPY t TO PROGRAM 'sh -c id'", "COPY TO PROGRAM"),
    ("INSERT INTO t VALUES (1)", "插入"),
    ("UPDATE t SET a = 1", "更新"),
    ("DELETE FROM t", "删除"),
    ("DROP TABLE t", "删表"),
    ("TRUNCATE TABLE t", "清表"),
    ("ALTER TABLE t ADD COLUMN c INT", "改结构"),
    ("CREATE TABLE t (a INT)", "建表"),
    ("SELECT * INTO newt FROM t", "SELECT INTO 落表"),
    ("SELECT * FROM t FOR UPDATE", "加锁读"),
    ("GRANT ALL ON t TO x", "授权"),
    ("", "空 SQL"),
    ("   ", "只有空白"),
]


@pytest.mark.parametrize("sql, label", ALLOWED, ids=[c[1] for c in ALLOWED])
def test_legitimate_reads_are_allowed(sql, label):
    ok, reason = is_read_only(sql)
    assert ok, f"{label} 被误拒：{reason}"


@pytest.mark.parametrize("sql, label", BLOCKED, ids=[c[1] for c in BLOCKED])
def test_writes_and_bypasses_are_blocked(sql, label):
    ok, _reason = is_read_only(sql)
    assert not ok, f"{label} 没被拦住"


def test_rejection_says_which_keyword():
    """报错要指出是哪个词，否则用户改不动自己的 SQL。"""
    ok, reason = is_read_only("SELECT * FROM t FOR UPDATE")
    assert not ok
    assert "update" in (reason or "").lower()


def test_token_based_check_does_not_regress_to_raw_text():
    """防回退：只要有人把判定改回在原文上跑正则，这条就红。

    用一条同时含三个禁用词的**纯字面量**查询——原文正则必然命中，token 判定必然放行。
    """
    sql = "SELECT '要 delete 还是 update 还是 create？' AS q"
    ok, reason = is_read_only(sql)
    assert ok, f"判定又退回原文匹配了：{reason}"
