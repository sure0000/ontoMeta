"""让 LLM 判定：这个键族到底是不是实体键，它是什么。

**分工**：机械层（``key_family``）负责召回与降噪——4968 个字段收敛成 12 个族；
本模块只问模型四件机械层做不了的事：

1. **判真伪**：``N<6>``/``310115`` 是行政区划码（``dimension_code``），
   ``A<2>N<8>``/``RY00000183`` 是人员编号（``entity_key``），``A<6>``/``IDCARD``
   是枚举值（``not_a_key``）。三者形状都合法，差别只在业务语义。
2. **命名实体**：从值形态 + 拼音列名读出「人员」——``bl_bh``/``zbr_bh``/``chujingr_bh``
   横跨 40 张表 52 种列名，只有模型能看出它们指同一个人。
3. **给谓词**：供两两关系命名，口径对齐 ``draft_generator`` 的关系命名提示词
   （三元组谓词，不许「关联」「生成」这类无信息量词）。
4. **给理由**：``reason`` 原样进候选行——人在画布上审的就是这句话。

**明确不问模型的**：基数（``key_family.cardinality_between`` 的算术）、URN 对齐
（``lineage_inventory.resolve``）、成员去留（闸门）。问了就是把能算准的东西交给概率。

**不降级**：没有 LLM、或返回不合格，一律抛错。这条是本仓的既有铁律——业务对象名
必须由模型给出中文语义名，绝不用 ``f_a2n8`` 这种技术标识顶替
（见 ``draft_generator.LlmNotConfiguredError`` 与 ``ObjectNamingIncompleteError``）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import settings
from app.services import llm_json
from app.services.common import make_async_http_client
from app.services.key_family import KeyFamily

logger = logging.getLogger("ontometa.key_family_verdict")

#: 判定结果三选一。
VERDICT_ENTITY_KEY = "entity_key"
VERDICT_DIMENSION_CODE = "dimension_code"
VERDICT_NOT_A_KEY = "not_a_key"
VERDICTS = frozenset({VERDICT_ENTITY_KEY, VERDICT_DIMENSION_CODE, VERDICT_NOT_A_KEY})

#: 每族给模型看的成员数上限。90 列的族全塞进去既费 token 又没有增量信息——
#: 判「这是不是人员编号」看 12 个成员和看 90 个是一样的。列名种类优先，覆盖面更广。
MAX_MEMBERS_IN_PROMPT = 12
#: 每族给模型看的代表值个数。
MAX_SAMPLES_IN_PROMPT = 5

_SYSTEM_PROMPT = (
    "你是数据治理专家。输入是若干「键族」——每个族是一组**值形状相同**、跨多张表复用的字段，"
    "由机械规则聚出来的候选实体键。你的任务是判定每个族的真实身份并命名。\n\n"
    "对每个族给出 verdict，三选一：\n"
    "- entity_key：这是某个**业务实体**的标识符，跨表复用即表示这些表都在谈同一个实体。"
    "例如身份证号、手机号、人员编号、案件编号、设备 IMEI。\n"
    "- dimension_code：这是**分类/编码体系**的取值，不是具体实体的标识。"
    "例如行政区划代码、国家代码、证件类型代码、行业分类码。它们也跨表复用，但连接的是"
    "「同一套码表」而不是「同一个实体」。\n"
    "- not_a_key：不是键。例如枚举值（IDCARD、居民身份证、汉族）、"
    "脱敏后的无意义值、明显是描述性内容的字段。\n\n"
    "**注意区分「随机串」与「不透明标识符」**：md5/sha/uuid 这类哈希值看起来像乱码，"
    "但它们恰恰是设备、账号、会话这类没有自然编号的实体最常用的标识符（如设备 gid、"
    "oaid、idfa、用户 uid）。判 not_a_key 的理由必须是「这个值不指向任何实体」，"
    "不能仅仅因为它不可读。\n\n"
    "value_shape 是值的形状签名：``A<n>`` 是 n 位字母、``N<n>`` 是 n 位数字，"
    "``H<n>`` 是 n 位十六进制摘要（32=md5、40=sha1、64=sha256），``U<36>`` 是 UUID。"
    "其余字符原样保留。\n\n"
    "判据优先看**值样例的形态**，其次看列名词元（可能是拼音缩写：bh=编号、hm=号码、"
    "dm=代码、sfz=身份证、dh=电话、aj=案件、ry=人员、jq=警情），再次看跨表分布"
    "（覆盖表数过多且区分度极低的，多半是码值不是实体键）。\n\n"
    "输出 JSON，只含一个字段 families（数组），每个元素必须包含：\n"
    "- family_id：原样回传输入里该族的 family_id（逐字保留，用于回链，不可省略或改写）。\n"
    "- verdict：entity_key / dimension_code / not_a_key 三者之一。\n"
    "- entity_name：该族代表的实体或码表的**中文**业务名（如「人员」「案件」「行政区划」）。"
    "verdict=not_a_key 时给空字符串。\n"
    "- key_name：这个键本身的**中文**名（如「人员编号」「身份证号」「行政区划代码」）。"
    "verdict=not_a_key 时给空字符串。\n"
    "- predicate：两张表通过该键关联时读得通的**中文谓词**，不超过 6 个汉字，"
    "要能读成「表A [谓词] 实体」（如「涉及」「归属」「登记」「使用」）。"
    "不要写「关联」「生成」「加工」这类无信息量、随便哪条都能套的默认词。"
    "verdict=not_a_key 时给空字符串。\n"
    "- confidence：0 到 1 的小数，表示你对该判定的把握。\n"
    "- reason：一句话说明判据（会原样展示给人工复核，要具体，说清你是看什么判的）。\n\n"
    "示例：\n"
    '- 输入 {family_id:"f_1", value_shape:"A<2>N<8>", samples:["RY00000183","RY00000330"], '
    'members:[{table:"aj_bl_zl",column:"bl_bh"},{table:"aj_cyry_ql",column:"ry_bh"}]} → '
    '{family_id:"f_1", verdict:"entity_key", entity_name:"人员", key_name:"人员编号", '
    'predicate:"涉及", confidence:0.88, reason:"值为 RY 前缀加 8 位序号的稳定编号；'
    '列名 bl_bh/ry_bh 均以 bh(编号)结尾，跨多张业务表复用同一套人员编号"}\n'
    '- 输入 {family_id:"f_2", value_shape:"N<6>", samples:["310115","320206"], ...} → '
    '{family_id:"f_2", verdict:"dimension_code", entity_name:"行政区划", '
    'key_name:"行政区划代码", predicate:"位于", confidence:0.9, '
    'reason:"6 位数字且前两位为省级代码，是国标行政区划编码，连接的是码表而非具体实体"}\n'
    '- 输入 {family_id:"f_3", value_shape:"A<6>", samples:["IDCARD","MILID"], ...} → '
    '{family_id:"f_3", verdict:"not_a_key", entity_name:"", key_name:"", predicate:"", '
    'confidence:0.85, reason:"取值是证件类型的英文枚举常量，不是标识某个实体的键"}\n'
    '- 输入 {family_id:"f_4", value_shape:"H<32>", '
    'samples:["66e31ba2a55eb1e8ff66f0be0db24af8","e92d66fa5cbd7141e9562878ce4bd54b"], '
    'members:[{table:"dmp_gid_ids",column:"gid"},'
    '{table:"dmp_device_portrait_merge_center",column:"gid"}]} → '
    '{family_id:"f_4", verdict:"entity_key", entity_name:"设备", key_name:"设备标识", '
    'predicate:"归属", confidence:0.85, reason:"32 位十六进制摘要，是设备的不透明标识符；'
    '列名 gid 在设备画像与设备 ID 映射表中复用同一套取值，指向同一台设备"}'
)


class LlmNotConfiguredError(RuntimeError):
    """没有可用的 LLM，键族判定无从谈起——直接失败并提示，不做规则降级。"""

    def __init__(self) -> None:
        super().__init__(
            "未配置可用的 LLM 服务，无法判定键族。"
            "请到【设置 → 依赖组件 → LLM】配置并测试连接后重试。"
        )


class VerdictIncompleteError(RuntimeError):
    """模型没有把每个族都判到，或判定不合格。

    不合格 = verdict 不在三个取值里、回链不上 family_id、或 entity_key/dimension_code
    却给不出中文名。**不用族 id 或值形状顶替名字**——那等于把「f_a2n8」当业务名写进库。
    """


@dataclass(frozen=True)
class FamilyVerdict:
    """一个族的判定结果。"""

    family_id: str
    verdict: str
    entity_name: str
    key_name: str
    predicate: str
    confidence: float
    reason: str

    @property
    def is_key(self) -> bool:
        """是键（实体键或码表键）——``not_a_key`` 的族不产出任何关系候选。"""
        return self.verdict in (VERDICT_ENTITY_KEY, VERDICT_DIMENSION_CODE)


def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text or "")


def family_payload(family: KeyFamily) -> dict:
    """一个族给模型看的样子。成员按**列名种类**去重后再截断，覆盖面优先。"""
    seen_columns: set[str] = set()
    members: list[dict] = []
    for member in family.members:
        if member.column.lower() in seen_columns:
            continue
        seen_columns.add(member.column.lower())
        members.append(
            {
                "table": member.table,
                "column": member.column,
                "distinct": member.distinct,
                "rows": member.rows,
            }
        )
        if len(members) >= MAX_MEMBERS_IN_PROMPT:
            break
    return {
        "family_id": family.id,
        "value_shape": family.value_shape,
        "samples": list(family.sample_values[:MAX_SAMPLES_IN_PROMPT]),
        "table_count": len(family.tables),
        "column_count": len(family.members),
        "distinct_column_names": len(family.column_names),
        "members": members,
    }


def _parse(raw: dict, families: list[KeyFamily]) -> list[FamilyVerdict]:
    by_id = {family.id: family for family in families}
    items = raw.get("families")
    if not isinstance(items, list):
        raise VerdictIncompleteError(
            f"LLM 返回里没有 families 数组（拿到的键：{sorted(raw)[:6]}）"
        )

    verdicts: dict[str, FamilyVerdict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        family_id = str(item.get("family_id") or "").strip()
        if family_id not in by_id:
            logger.warning("LLM 回了一个对不上的 family_id=%s，跳过", family_id)
            continue
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in VERDICTS:
            raise VerdictIncompleteError(
                f"族 {family_id} 的 verdict 不合法：{verdict!r}（应为 {sorted(VERDICTS)}）"
            )
        entity_name = str(item.get("entity_name") or "").strip()
        key_name = str(item.get("key_name") or "").strip()
        if verdict != VERDICT_NOT_A_KEY and not (
            _has_chinese(entity_name) and _has_chinese(key_name)
        ):
            # 判成键却给不出中文名 = 这次没完成命名任务。不用 family_id 顶替。
            raise VerdictIncompleteError(
                f"族 {family_id} 判为 {verdict} 却缺中文命名"
                f"（entity_name={entity_name!r}, key_name={key_name!r}）"
            )
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        verdicts[family_id] = FamilyVerdict(
            family_id=family_id,
            verdict=verdict,
            entity_name=entity_name,
            key_name=key_name,
            predicate=str(item.get("predicate") or "").strip(),
            confidence=min(max(confidence, 0.0), 1.0),
            reason=str(item.get("reason") or "").strip(),
        )

    missing = [family.id for family in families if family.id not in verdicts]
    if missing:
        raise VerdictIncompleteError(
            f"LLM 漏判了 {len(missing)} 个族：{missing[:5]}"
            f"{'…' if len(missing) > 5 else ''}"
        )
    return [verdicts[family.id] for family in families]


async def judge_families(
    families: list[KeyFamily], runtime_config=None
) -> list[FamilyVerdict]:
    """一次请求判完整批族。族数是十几个量级（不是几万对），装得下，不分块。

    没配 LLM 抛 :class:`LlmNotConfiguredError`；返回不合格抛
    :class:`VerdictIncompleteError`——两者都**不降级**。
    """
    if not families:
        return []

    if runtime_config is None:
        api_key, base_url, model = settings.openai_api_key, None, settings.openai_model
    else:
        api_key = runtime_config.api_key
        base_url = runtime_config.api_base_url
        model = runtime_config.model
    if not api_key:
        raise LlmNotConfiguredError()

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        # 整域一次长生成，用不了通用的 llm_timeout_seconds——见该设置项的说明。
        timeout=settings.key_family_verdict_timeout_seconds,
        max_retries=settings.llm_max_retries,
        http_client=make_async_http_client(),
    )
    payload = {"families": [family_payload(family) for family in families]}
    started = time.perf_counter()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
    finally:
        await client.close()
    elapsed = time.perf_counter() - started

    choice = response.choices[0]
    content = getattr(choice.message, "content", None) or ""
    raw = llm_json.coerce_json_object(content, primary_list_key="families")
    verdicts = _parse(raw, families)
    logger.info(
        "键族判定完成：%d 个族 → 实体键 %d / 码表 %d / 非键 %d（耗时 %.1fs）",
        len(verdicts),
        sum(v.verdict == VERDICT_ENTITY_KEY for v in verdicts),
        sum(v.verdict == VERDICT_DIMENSION_CODE for v in verdicts),
        sum(v.verdict == VERDICT_NOT_A_KEY for v in verdicts),
        elapsed,
    )
    return verdicts
