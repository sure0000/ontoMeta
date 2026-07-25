import re

from app.schemas import (
    DataHubDomainBundle,
    EvidenceBundle,
    LogicEvidencePack,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
    RelationEvidencePack,
)
from app.services.object_classifier import FieldSignal, classify_object_role
from app.services.relation_terms import infer_relation_term
from app.services.relation_structure import infer_relation_structure_type


def _to_snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


# 技术/系统字段词元（独立于表名的内容信号）：命中则该字段偏技术/安全/
# 基础设施，而非业务属性。用于区分 auth/config/session 等系统表。
_TECHNICAL_FIELD_TOKENS = (
    "token", "secret", "passwd", "password", "pwd", "salt", "hash", "cipher",
    "cert", "credential", "nonce", "signature", "encrypt", "decrypt",
    "apikey", "api_key", "access_key", "secret_key", "private_key", "public_key",
    "refresh", "jwt", "oauth", "ldap", "session", "cookie", "ticket",
    "acl", "privilege", "permission", "scope", "grant",
    "config", "setting", "param", "checksum", "crc", "trace", "span",
    "cache", "lock", "ttl", "cursor", "offset", "seq", "uuid", "guid",
)


def _is_technical_field(name: str) -> bool:
    """字段名（已 lower）是否命中技术词元。用分隔符拆词后比对，避免误伤
    如 seq_no 中的 seq 与 business 中的子串（business 不含独立词元）。"""
    tokens = set(re.split(r"[^a-z0-9]+", name))
    for token in _TECHNICAL_FIELD_TOKENS:
        if token in tokens:
            return True
        # 无分隔符的紧凑命名（如 accesstoken）回退到子串包含，但限较长词元避免误伤。
        if len(token) >= 6 and token in name:
            return True
    return False


def _infer_object_name(dataset_name: str) -> str:
    base = _to_snake(dataset_name)
    if base.endswith("s"):
        return base
    return f"{base}_entity" if not base.endswith("_entity") else base


class EvidenceBuilder:
    """将 DataHub 原始输入整理为 LLM 证据包。"""

    def build(
        self,
        bundle: DataHubDomainBundle,
        *,
        include_business_logics: bool = False,
    ) -> EvidenceBundle:
        object_types: list[ObjectTypeEvidencePack] = []
        properties: list[PropertyEvidencePack] = []
        relations: list[RelationEvidencePack] = []
        business_logics: list[LogicEvidencePack] = []

        dataset_name_map = {ds.urn: ds.name for ds in bundle.datasets}
        # 跨表拓扑：先聚合“每张表被多少张其它表通过外键指向”（入度）
        # 与血缘上/下游数量，供对象角色分类器使用。
        fk_in_degree, lineage_up, lineage_down = self._build_topology(bundle)

        # 关系描述用的业务展示名映射:候选名(source_object/target_object)必须
        # 仍由技术名(ds.name)推导，保证与 object_types 的 candidate_name 一致；
        # 但描述文本改用业务展示名，让 LLM 拿到的关系语义证据是「订单明细
        # 加工至 结算汇总」而非「order_di_entity 加工至 settlement_1d_entity」，
        # 才有足够信息推断出具体业务关系词，而不是笼统落回「派生」。
        dataset_display_by_urn = {
            ds.urn: (ds.display_name or ds.name) for ds in bundle.datasets
        }
        dataset_by_name = {ds.name: ds for ds in bundle.datasets}

        for dataset in bundle.datasets:
            object_name = _infer_object_name(dataset.name)
            # 业务命名锚定：是否有人工赋予的业务名/描述/术语，而非裸技术表名。
            has_business_naming = bool(
                (dataset.display_name and dataset.display_name != dataset.name)
                or dataset.description
                or dataset.glossary_terms
            )
            role = classify_object_role(
                [
                    FieldSignal(
                        name=f.name,
                        semantic_type=self._infer_semantic_type(f),
                        is_primary_key=f.is_primary_key,
                        is_foreign_key=f.is_foreign_key,
                        unique_count=f.unique_count,
                    )
                    for f in dataset.fields
                ],
                fk_in_degree=fk_in_degree.get(dataset.name, 0),
                lineage_upstream=lineage_up.get(dataset.urn, 0),
                lineage_downstream=lineage_down.get(dataset.urn, 0),
                glossary_terms=dataset.glossary_terms,
                row_count=dataset.row_count,
                has_business_naming=has_business_naming,
                subtypes=dataset.subtypes,
                tags=dataset.tags,
            )
            # 保留原启发式（维表）作为命名置信度；对象是否为业务对象另走 role。
            is_dimension = dataset.name.startswith("dim_") or "维" in (dataset.display_name or "")
            confidence = 0.85 if is_dimension else 0.65

            object_types.append(
                ObjectTypeEvidencePack(
                    candidate_name=object_name,
                    display_name=dataset.display_name or dataset.name,
                    description=dataset.description,
                    source_dataset_urn=dataset.urn,
                    confidence=confidence,
                    evidence_refs=[dataset.urn, bundle.domain.id],
                    table_role=role.role,
                    role_confidence=role.confidence,
                    role_reason=role.reason,
                )
            )

            for field in dataset.fields:
                semantic = self._infer_semantic_type(field)
                properties.append(
                    PropertyEvidencePack(
                        object_candidate_name=object_name,
                        field_name=field.name,
                        display_name=field.display_name or field.name,
                        description=field.description,
                        data_type=field.data_type,
                        semantic_type=semantic,
                        sample_values=field.sample_values,
                        confidence=0.7 if field.display_name else 0.55,
                        evidence_refs=[f"{dataset.urn}#{field.name}"],
                    )
                )

                if field.is_foreign_key and field.foreign_key_target:
                    target_table = field.foreign_key_target.split(".")[0]
                    target_object = _infer_object_name(target_table)
                    source_label = dataset.display_name or dataset.name
                    target_ds = dataset_by_name.get(target_table)
                    target_label = (
                        (target_ds.display_name or target_ds.name)
                        if target_ds
                        else target_table
                    )
                    relations.append(
                        RelationEvidencePack(
                            name=f"{object_name}_to_{target_object}",
                            display_name=infer_relation_term("foreign_key", field.name),
                            source_object=object_name,
                            target_object=target_object,
                            cardinality="many_to_one",
                            structure_type="foreign_key",
                            description=(
                                f"{source_label} 通过外键 {field.name} 关联 {target_label}"
                                f"（{field.foreign_key_target}）"
                            ),
                            confidence=0.8,
                            evidence_refs=[f"{dataset.urn}#{field.name}"],
                        )
                    )

        for lineage in bundle.lineages:
            source_name = dataset_name_map.get(lineage.source_urn, lineage.source_urn)
            target_name = dataset_name_map.get(lineage.target_urn, lineage.target_urn)
            source_obj = _infer_object_name(source_name)
            target_obj = _infer_object_name(target_name)
            # 描述用业务展示名(取不到时才退回技术名)，供 LLM 与确定性兜底推断使用。
            source_label = dataset_display_by_urn.get(lineage.source_urn, source_name)
            target_label = dataset_display_by_urn.get(lineage.target_urn, target_name)
            relations.append(
                RelationEvidencePack(
                    name=f"{source_obj}_feeds_{target_obj}",
                    display_name=infer_relation_term("lineage", target_label=target_label),
                    source_object=source_obj,
                    target_object=target_obj,
                    cardinality="one_to_many",
                    structure_type="derivation",
                    description=f"血缘：{source_label} 加工至 {target_label}",
                    confidence=0.6,
                    evidence_refs=[lineage.source_urn, lineage.target_urn],
                )
            )

        if include_business_logics:
            for logic in bundle.logic_evidences:
                logic_type = "metric" if logic.name in {"gmv", "revenue", "amount"} else "tag"
                business_logics.append(
                    LogicEvidencePack(
                        name=_to_snake(logic.name),
                        display_name=logic.name,
                        logic_type=logic_type,
                        description=logic.description,
                        expression_summary=logic.expression,
                        source_type=logic.source_type,
                        source_ref=logic.source_ref,
                        confidence=0.65,
                        evidence_refs=[logic.source_ref or logic.name],
                    )
                )

        # 方向校正：折叠方向相反的重复关系为单条有向关系。DataHub 常把 ERPNext
        # “Link” 等引用型外键按双向血缘导入，产生 A→B 与 B→A 两条重复血缘，
        # 被前端渲染成“双向”。业务引用其实是单向的（明细/事实表引用主数据/维度表）。
        row_count_by_object = {
            _infer_object_name(ds.name): ds.row_count for ds in bundle.datasets
        }
        relations = self._collapse_reverse_relations(relations, row_count_by_object)

        return EvidenceBundle(
            object_types=object_types,
            properties=properties,
            relations=relations,
            business_logics=business_logics,
        )

    def _build_topology(
        self, bundle: DataHubDomainBundle
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """聚合跨表拓扑信号（不依赖表名含义）：

        - fk_in_degree: 按表名计数“有多少张不同的表通过外键指向它”。
        - lineage_up / lineage_down: 按 URN 计数血缘上/下游数量。
        """
        fk_in_degree: dict[str, set[str]] = {}
        for ds in bundle.datasets:
            for f in ds.fields:
                if f.is_foreign_key and f.foreign_key_target:
                    target_table = f.foreign_key_target.split(".")[0]
                    fk_in_degree.setdefault(target_table, set()).add(ds.name)
        fk_counts = {table: len(refs) for table, refs in fk_in_degree.items()}

        lineage_up: dict[str, int] = {}
        lineage_down: dict[str, int] = {}
        for lin in bundle.lineages:
            lineage_down[lin.source_urn] = lineage_down.get(lin.source_urn, 0) + 1
            lineage_up[lin.target_urn] = lineage_up.get(lin.target_urn, 0) + 1
        return fk_counts, lineage_up, lineage_down

    def _collapse_reverse_relations(
        self,
        relations: list[RelationEvidencePack],
        row_count_by_object: dict[str, int | None],
    ) -> list[RelationEvidencePack]:
        """把方向相反的重复关系折叠为单条有向关系。

        DataHub 常将 ERPNext“Link”等引用型外键按双向血缘导入，同一对对象间因此
        同时生成 A→B 与 B→A 两条 derivation 关系，被前端渲染成“双向”。但业务
        引用其实是单向的（明细/事实表 → 主数据/维度表）。按优先级择一保留：

        1. 外键方向权威：若该对存在 foreign_key 关系，以其方向为准，丢弃反向血缘；
           若两个方向都是真实外键（互相引用）则保留双向。
        2. 行数：明细表（行数多）引用主数据表（行数少），行数多的一侧为源、少的为目标。
        3. 关联度：行数缺失时，被更多表关联的一侧更像主数据/维度表，作为目标。
        4. 仍无法区分时按对象名字典序稳定保留一条，避免随机。

        同方向的多条关系（如同一表的两个不同外键都指向 country）不受影响；只有方向
        相反的重复才被折叠。
        """
        if not relations:
            return relations

        # 无向关联度：每个对象关联到多少个不同对象（主数据/维度被更多表关联）。
        neighbors: dict[str, set[str]] = {}
        groups: dict[frozenset[str], list[RelationEvidencePack]] = {}
        for rel in relations:
            neighbors.setdefault(rel.source_object, set()).add(rel.target_object)
            neighbors.setdefault(rel.target_object, set()).add(rel.source_object)
            groups.setdefault(
                frozenset((rel.source_object, rel.target_object)), []
            ).append(rel)
        degree = {obj: len(peers) for obj, peers in neighbors.items()}

        kept: list[RelationEvidencePack] = []
        for pair, rels in groups.items():
            # 自关联或只有单一方向：原样保留。
            directions = {(r.source_object, r.target_object) for r in rels}
            if len(pair) < 2 or len(directions) == 1:
                kept.extend(rels)
                continue
            fk_dirs = {
                (r.source_object, r.target_object)
                for r in rels
                if r.structure_type == "foreign_key"
            }
            # 两侧都是真实外键 → 互相引用，保留双向。
            if len(fk_dirs) >= 2:
                kept.extend(rels)
                continue
            canonical = self._orient_relation(
                pair, fk_dirs, row_count_by_object, degree
            )
            kept.extend(
                r for r in rels if (r.source_object, r.target_object) == canonical
            )
        return kept

    @staticmethod
    def _orient_relation(
        pair: frozenset[str],
        fk_dirs: set[tuple[str, str]],
        row_count_by_object: dict[str, int | None],
        degree: dict[str, int],
    ) -> tuple[str, str]:
        """为双向重复关系确定唯一业务方向（源 → 目标）。"""
        a, b = sorted(pair)
        # 1) 外键方向权威。
        if len(fk_dirs) == 1:
            return next(iter(fk_dirs))
        # 2) 行数：多行（明细）→ 少行（主数据）。
        ra, rb = row_count_by_object.get(a), row_count_by_object.get(b)
        if ra is not None and rb is not None and ra != rb:
            return (a, b) if ra > rb else (b, a)
        # 3) 关联度：度小的作源，度大的（主数据）作目标。
        da, db = degree.get(a, 0), degree.get(b, 0)
        if da != db:
            return (a, b) if da < db else (b, a)
        # 4) 稳定兑底：字典序 a → b。
        return (a, b)

    def _infer_semantic_type(self, field) -> str:
        name = field.name.lower()
        if field.is_primary_key or name.endswith("_id"):
            return "identifier"
        # 技术词汇字段（token/secret/hash/config/session 等）：内容信号，不看表名，
        # 供分类器识别技术/系统表。放在 datetime/amount 之前，避免 expires_at
        # 落到 datetime、避免其他技术字段落到默认 attribute。
        if _is_technical_field(name):
            return "technical"
        if "date" in name or "time" in name:
            return "datetime"
        if "amount" in name or "price" in name:
            return "amount"
        if "status" in name or "type" in name or "level" in name:
            return "category"
        if "tag" in name or name.startswith("is_"):
            return "flag"
        return "attribute"

