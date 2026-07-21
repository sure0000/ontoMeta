"""业务逻辑代码导入服务:粘贴 SQL/代码 → LLM/Mock 解析 → 业务逻辑草稿。

业务逻辑与工作区/本体草稿分离,归属于数据域的「已发布本体」。
本服务仅生成业务逻辑的定义(名称/类型/表达式/描述),引用对象与字段的挑选
由用户在业务逻辑详情页手动完成,发布后引用固化为绑定。
"""

import json
import re

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BusinessLogic, DomainContext, EntityStatus, Ontology
from app.schemas import BusinessLogicDetail
from app.services.common import make_http_client


_CODE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME_FROM_COMMENT_RE = re.compile(
    r"(?:--|#|//)\s*(?:name|名称|逻辑名|logic[_ ]?name)\s*[:：]\s*(.+)",
    re.IGNORECASE,
)
_METRIC_KEYWORDS = (
    "sum(",
    "count(",
    "avg(",
    "min(",
    "max(",
    "sum (",
    "count (",
)
_TAG_KEYWORDS = ("case when", "label", "tag", "is_", "flag")
_RULE_KEYWORDS = ("if ", "when ", "rule", "constraint", "check(", "assert")


def _truncate(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n..."


def _slugify(name: str, fallback: str) -> str:
    tokens = _CODE_TOKEN_RE.findall(name or "")
    if not tokens:
        tokens = _CODE_TOKEN_RE.findall(fallback or "")
    if not tokens:
        return "business_logic"
    slug = "_".join(t.lower() for t in tokens)
    return slug[:80]


def _extract_display_from_comment(code: str) -> str | None:
    for line in code.splitlines():
        m = _NAME_FROM_COMMENT_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def _infer_logic_type(code: str) -> str:
    lowered = code.lower()
    if any(k in lowered for k in _TAG_KEYWORDS):
        return "tag"
    if any(k in lowered for k in _RULE_KEYWORDS):
        return "rule"
    if any(k in lowered for k in _METRIC_KEYWORDS):
        return "metric"
    return "metric"


def _first_meaningful_line(code: str) -> str:
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("--", "#", "//")):
            continue
        return stripped
    return code.strip().splitlines()[0] if code.strip() else "business_logic"


class LogicImportService:
    """从粘贴的代码文本解析生成业务逻辑草稿。

    Mock 模式下使用规则启发式;LLM 模式下调用 OpenAI 兼容接口要求返回结构化 JSON。
    构造方式与 OntologyDraftGenerator 一致,优先使用 SettingsService 配置的默认 LLM。
    """

    def __init__(self, runtime_config=None) -> None:
        if runtime_config is None:
            self.use_mock = settings.use_mock_llm or not settings.openai_api_key
            self.client = (
                OpenAI(
                    api_key=settings.openai_api_key,
                    http_client=make_http_client(),
                )
                if not self.use_mock
                else None
            )
            self.model = settings.openai_model
        else:
            self.use_mock = runtime_config.use_mock or not runtime_config.api_key
            self.client = (
                OpenAI(
                    api_key=runtime_config.api_key,
                    base_url=runtime_config.api_base_url,
                    http_client=make_http_client(),
                )
                if not self.use_mock
                else None
            )
            self.model = runtime_config.model

    async def import_from_code(
        self,
        db: Session,
        *,
        domain_id: str,
        code: str,
        source_type: str = "sql",
        operator: str | None = None,
    ) -> BusinessLogicDetail:
        from app.services.query import OntologyQueryService

        ontology = self._resolve_published_ontology(db, domain_id)
        code = (code or "").strip()
        if not code:
            raise ValueError("待解析的代码不能为空")

        if self.use_mock:
            parsed = self._parse_with_mock(code, source_type)
        else:
            parsed = await self._parse_with_llm(db, ontology, code, source_type)

        name = self._ensure_unique_name(db, ontology.id, parsed["name"])

        logic = BusinessLogic(
            ontology_id=ontology.id,
            name=name,
            display_name=parsed["display_name"],
            logic_type=parsed["logic_type"],
            description=parsed.get("description"),
            expression_summary=parsed.get("expression_summary") or _truncate(code),
            source_type=source_type,
            source_ref="code_import",
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
        )
        db.add(logic)
        db.flush()
        self._log_change(db, logic.id, "import", operator, f"从代码导入业务逻辑:{logic.display_name}")
        db.commit()

        detail = OntologyQueryService().get_business_logic(db, logic.id)
        if not detail:
            raise ValueError("Business logic not found after import")
        return detail

    def _resolve_published_ontology(self, db: Session, domain_id: str) -> Ontology:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        from app.services.query import OntologyQueryService

        ontology = OntologyQueryService().get_published_ontology(db, domain_id)
        if not ontology:
            raise ValueError("该数据域尚无已发布本体,无法创建业务逻辑")
        return ontology

    def _ensure_unique_name(self, db: Session, ontology_id: str, name: str) -> str:
        base = name or "business_logic"
        candidate = base
        suffix = 1
        while (
            db.query(BusinessLogic)
            .filter(
                BusinessLogic.ontology_id == ontology_id,
                BusinessLogic.name == candidate,
            )
            .first()
        ):
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    @staticmethod
    def _log_change(
        db: Session,
        logic_id: str,
        action: str,
        operator: str | None,
        summary: str | None,
    ) -> None:
        from app.models import EntityChangeLog

        db.add(
            EntityChangeLog(
                entity_type="business_logic",
                entity_id=logic_id,
                action=action,
                operator=operator,
                change_summary=summary,
            )
        )

    # --- Mock ---

    def _parse_with_mock(self, code: str, source_type: str) -> dict:
        display = _extract_display_from_comment(code) or _derive_display(code)
        name = _slugify(display, _first_meaningful_line(code))
        logic_type = _infer_logic_type(code)
        description = _extract_display_from_comment(code) or _derive_description(code)
        return {
            "name": name,
            "display_name": display,
            "logic_type": logic_type,
            "description": description,
            "expression_summary": _truncate(code),
        }

    # --- LLM ---

    async def _parse_with_llm(
        self,
        db: Session,
        ontology: Ontology,
        code: str,
        source_type: str,
    ) -> dict:
        context = self._build_ontology_context(db, ontology)
        prompt = json.dumps(
            {"source_type": source_type, "code": code, "ontology_context": context},
            ensure_ascii=False,
            indent=2,
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业业务逻辑建模专家。根据用户粘贴的代码(SQL/Python/其它)和"
                        "已发布本体的对象/字段清单,解析出一条业务逻辑的定义。\n\n"
                        "返回 JSON,字段:\n"
                        "- name: 英文标识名(snake_case,简洁)\n"
                        "- display_name: 中文业务语义名称\n"
                        "- logic_type: metric | tag | rule\n"
                        "- description: 一句话说明该逻辑的业务含义\n"
                        "- expression_summary: 简化的计算/规则表达式摘要(可截断)\n\n"
                        "只解析定义,不要返回对象/字段绑定(由用户在编辑页手动挑选)。\n"
                        "若代码无法明确判断类型,默认 metric。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        return self._normalize_llm_output(raw, code)

    def _build_ontology_context(self, db: Session, ontology: Ontology) -> dict:
        from app.models import ObjectType

        objects = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id)
            .order_by(ObjectType.name)
            .all()
        )
        return {
            "ontology_id": ontology.id,
            "object_types": [
                {"name": o.name, "display_name": o.display_name}
                for o in objects
            ],
        }

    def _normalize_llm_output(self, raw: dict, code: str) -> dict:
        name = (raw.get("name") or "").strip()
        display = (raw.get("display_name") or "").strip()
        logic_type = (raw.get("logic_type") or "").strip().lower()
        if logic_type not in {"metric", "tag", "rule"}:
            logic_type = _infer_logic_type(code)
        if not name:
            name = _slugify(display, _first_meaningful_line(code))
        if not display:
            display = _derive_display(code)
        return {
            "name": name,
            "display_name": display,
            "logic_type": logic_type,
            "description": (raw.get("description") or "").strip() or None,
            "expression_summary": (raw.get("expression_summary") or "").strip() or _truncate(code),
        }


def _derive_display(code: str) -> str:
    first = _first_meaningful_line(code)
    m = _CODE_TOKEN_RE.search(first)
    if not m:
        return "业务逻辑"
    token = m.group(0)
    # 简单美化:下划线分词后取主体
    parts = [p for p in token.split("_") if p]
    if not parts:
        return token
    return parts[-1]


def _derive_description(code: str) -> str:
    first = _first_meaningful_line(code)
    return f"由代码导入:{first[:60]}"
