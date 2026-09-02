import re

from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    EvidenceBundle,
    LogicEvidencePack,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
    RelationEvidencePack,
)
from app.services.object_classifier import (
    ROLE_BUSINESS_OBJECT,
    FieldSignal,
    classify_object_role,
)
from app.services.bridge_collapse import select_bridge_endpoints
from app.services.community_detection import label_propagation_clusters
from app.services.fact_naming import detect_fact_name, detect_weak_fact_name
from app.services.relation_terms import infer_relation_term, reference_term
from app.services.source_profile import InferredFk, SourceProfile, detect_source_profile


def _to_snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


# 桥表塌缩↔智能重判的迭代轮数上限：重判某桥表为业务对象可能让引用它的桥表凑出端点，
# 故迭代到稳定；上限兜底防御（实测有效轮数很少）。
_MAX_RECLASSIFY_ROUNDS = 4


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


# 名字猜出来的这几类语义会**决定目标列的物理类型**（datetime→TIMESTAMP、amount→DECIMAL、
# flag→BOOLEAN，见各 Dialect Adapter 的 map_type）。猜错的代价不是标签难看，而是物化出一张
# 装不下自己源数据的表：ERP 域里 date_format/time_format（VARCHAR，存 "dd-mm-yyyy"）被判
# datetime、_user_tags（TEXT，存 ["a"]）被判 flag，搬运每次都挂在类型转换上。
_PHYSICAL_SEMANTICS = frozenset({"datetime", "amount", "flag"})
_TEXT_TYPE_TOKENS = ("text", "char", "string", "json", "blob", "clob")

_TRUEISH = frozenset({"1", "0", "true", "false", "t", "f", "yes", "no", "y", "n"})


def _is_text_physical(data_type: str | None) -> bool:
    """物理类型是不是文本类。真实源给的是 ``VARCHAR(140)`` 这种带参数的原样类型。"""
    lowered = (data_type or "").lower()
    return any(token in lowered for token in _TEXT_TYPE_TOKENS)


def _samples_support(semantic: str, samples: list[str] | None) -> bool:
    """样例值是否支持这个语义判断。没有样例 → False（无证据即不支持）。

    「字符串里存的是日期」是常见且合理的（源库偷懒），此时判 datetime 让目标列升级成
    TIMESTAMP 是**对的**；而 "dd-mm-yyyy" 这种格式串则不是。两者只有看值才分得开。
    """
    values = [str(v).strip() for v in (samples or []) if str(v).strip()]
    if not values:
        return False
    if semantic == "flag":
        return all(v.lower() in _TRUEISH for v in values)
    if semantic == "amount":
        for value in values:
            try:
                float(value.replace(",", ""))
            except ValueError:
                return False
        return True
    # datetime：至少要以「4 位年 + 分隔符」开头，且含数字分隔——排除 dd-mm-yyyy 这类格式串。
    return all(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", v) for v in values)


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
        # 桥表智能重判所需：每个对象的分类信号（抑制 fact/child 信号后可重跑分类器，
        # 判断「若不是关系表，它是业务对象还是数据表」）。
        field_signals_by_name: dict[str, list[FieldSignal]] = {}
        reeval_args_by_name: dict[str, dict] = {}
        # 子表锚点是**从源里读出来的事实**（Frappe 的 parent/parenttype 列），不是推断。
        # 桥表重判时必须能拿到它——否则硬事实会被「单列主键」这条软信号覆盖，见
        # _reclassify_bridge_to_object。
        child_table_by_name: dict[str, bool] = {}

        dataset_name_map = {ds.urn: ds.name for ds in bundle.datasets}
        # 源画像：把 Frappe 等源的建库约定翻译成 PK/FK/子表/系统列信号。默认画像
        # 为无操作（声明式元数据原样透传），保持既有行为。
        profile = detect_source_profile(bundle)
        table_index = profile.build_table_index(bundle)
        # 每张表的推断外键边（Frappe Link 字段等）。声明式外键仍走原字段属性。
        inferred_fk_by_name: dict[str, list[InferredFk]] = {
            ds.name: profile.inferred_fks(ds, table_index) for ds in bundle.datasets
        }
        # 跨表拓扑：先聚合“每张表被多少张其它表通过外键指向”（入度）
        # 与血缘上/下游数量，供对象角色分类器使用。（含源画像推断的外键边）
        fk_in_degree, lineage_up, lineage_down, fk_out_degree, segment_size = (
            self._build_topology(bundle, inferred_fk_by_name)
        )

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
            pk_names = profile.primary_key_names(dataset)
            inferred_fk_cols = {
                e.column.lower() for e in inferred_fk_by_name.get(dataset.name, [])
            }
            is_child = profile.is_child_table(dataset)
            # 事实/动词命名信号：技术表名 + 业务含义（展示名/描述/术语）里是否含事件
            # 动词（xx调整、xx交易）。命中则这张表记录一次业务事实而非实体，分类器据此
            # 复用 bridge 改判并标待复核（详见 fact_naming）。
            fact_meaning = " ".join(
                t
                for t in (
                    dataset.display_name,
                    dataset.description,
                    *(dataset.glossary_terms or []),
                )
                if t
            )
            fact_name_token = detect_fact_name(dataset.name, fact_meaning)
            # 弱事实/交易命名（订单/发票/工单/order/invoice）：单凭命名不判事实，
            # 由分类器结合结构证据（多维度外键 + 度量字段）决定是否改判关系表。
            weak_fact_name_token = detect_weak_fact_name(dataset.name, fact_meaning)
            # 构造分类信号：按源画像补齐 PK/FK，并剥离框架系统列（审计/子表锚点等），
            # 使度量/描述占比反映真实业务字段而非框架噪声。主键即便属系统列也保留。
            field_signals: list[FieldSignal] = []
            for f in dataset.fields:
                is_pk = f.is_primary_key or f.name.lower() in pk_names
                if not is_pk and profile.is_system_column(f.name):
                    continue
                field_signals.append(
                    FieldSignal(
                        name=f.name,
                        semantic_type=self._infer_semantic_type(f),
                        is_primary_key=is_pk,
                        is_foreign_key=f.is_foreign_key
                        or f.name.lower() in inferred_fk_cols,
                        unique_count=f.unique_count,
                    )
                )
            role = classify_object_role(
                field_signals,
                fk_in_degree=fk_in_degree.get(dataset.name, 0),
                distinct_fk_targets=fk_out_degree.get(dataset.name, 0),
                lineage_upstream=lineage_up.get(dataset.urn, 0),
                lineage_downstream=lineage_down.get(dataset.urn, 0),
                glossary_terms=dataset.glossary_terms,
                row_count=dataset.row_count,
                has_business_naming=has_business_naming,
                subtypes=dataset.subtypes,
                tags=dataset.tags,
                is_child_table=is_child,
                fact_name_token=fact_name_token,
                weak_fact_name_token=weak_fact_name_token,
                segment_size=segment_size.get(dataset.name),
            )
            # 存一份分类信号（不含 fact/weak_fact 这些把它推向 bridge 的**软**信号），
            # 供未能塌缩的桥表「智能重判为对象」时抑制它们后重跑分类器。
            # is_child_table 是源事实不是软信号，单独存在 child_table_by_name 里，
            # 重判时必须尊重它——见 _reclassify_bridge_to_object。
            field_signals_by_name[object_name] = field_signals
            child_table_by_name[object_name] = is_child
            reeval_args_by_name[object_name] = dict(
                fk_in_degree=fk_in_degree.get(dataset.name, 0),
                distinct_fk_targets=fk_out_degree.get(dataset.name, 0),
                lineage_upstream=lineage_up.get(dataset.urn, 0),
                lineage_downstream=lineage_down.get(dataset.urn, 0),
                glossary_terms=dataset.glossary_terms,
                row_count=dataset.row_count,
                has_business_naming=has_business_naming,
                subtypes=dataset.subtypes,
                tags=dataset.tags,
                segment_size=segment_size.get(dataset.name),
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
                    row_count=dataset.row_count,
                    table_role=role.role,
                    role_confidence=role.confidence,
                    role_reason=role.reason,
                    needs_review=role.needs_review,
                    role_signals={
                        "score": role.score,
                        "needs_review": role.needs_review,
                        "role": role.role,
                        "signals": role.signals,
                        # 弃权标记随证据快照落库：仲裁要靠它区分「判成业务对象」
                        # 与「没判出来，兜底成业务对象」。
                        **({"abstained": True} if role.abstained else {}),
                    },
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
                        unique_count=field.unique_count,
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

            # 源画像推断的外键（如 Frappe Link 字段：列名命中某 DocType 名）。
            # 声明式外键为空时，这是恢复关系图与 fk 拓扑的主要来源。标注为推断、
            # 置信度略低，交由人工/LLM 复核。
            declared_fk_cols = {
                f.name for f in dataset.fields if f.is_foreign_key and f.foreign_key_target
            }
            for edge in inferred_fk_by_name.get(dataset.name, []):
                if edge.column in declared_fk_cols:
                    continue  # 已有声明式外键，避免重复
                target_object = _infer_object_name(edge.target_table)
                source_label = dataset.display_name or dataset.name
                target_ds = dataset_by_name.get(edge.target_table)
                target_label = (
                    (target_ds.display_name or target_ds.name)
                    if target_ds
                    else edge.target_table
                )
                relations.append(
                    RelationEvidencePack(
                        name=f"{object_name}_to_{target_object}",
                        display_name=infer_relation_term("foreign_key", edge.column),
                        source_object=object_name,
                        target_object=target_object,
                        cardinality="many_to_one",
                        structure_type="foreign_key",
                        description=(
                            f"{source_label} 通过引用字段 {edge.column} 关联 {target_label}"
                            f"（推断，来源 {profile.name} 源画像）"
                        ),
                        confidence=0.6,
                        evidence_refs=[f"{dataset.urn}#{edge.column}"],
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
                    display_name=infer_relation_term(
                        "lineage",
                        target_label=target_label,
                        source_label=source_label,
                    ),
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

        # 桥表(关系表)塌缩 + 智能重判：能连接两个业务对象的桥表塌缩为 BO_A→BO_B 关系
        # （桥表作 mapping 实现表）；连不到两个业务对象的桥表本就不是关系，智能重判为对象
        # （业务对象/数据表）。重判可能让其它桥表凑出新端点，故迭代到稳定。见
        # bridge_collapse.select_bridge_endpoints 与 _collapse_and_reclassify_bridges。
        relations.extend(
            self._collapse_and_reclassify_bridges(
                bundle,
                object_types=object_types,
                profile=profile,
                table_index=table_index,
                inferred_fk_by_name=inferred_fk_by_name,
                fk_in_degree=fk_in_degree,
                row_count_by_object=row_count_by_object,
                field_signals_by_name=field_signals_by_name,
                reeval_args_by_name=reeval_args_by_name,
                child_table_by_name=child_table_by_name,
            )
        )

        # 业务关系精炼：rule 1（业务关系只存在于业务对象之间）+ 去除与 FK 重复的血缘 +
        # 把反向的「主数据 派生出 单据」翻转为「单据 引用 主数据」。角色/展示名由上方
        # 已生成的 object_types 汇总（两端候选名与对象候选名同由 _infer_object_name 推得）。
        role_by_object = {ot.candidate_name: ot.table_role for ot in object_types}
        label_by_object = {ot.candidate_name: ot.display_name for ot in object_types}
        relations = self._refine_business_relations(
            relations, role_by_object, label_by_object, row_count_by_object
        )

        return EvidenceBundle(
            object_types=object_types,
            properties=properties,
            relations=relations,
            business_logics=business_logics,
        )

    def _bridge_ref_targets(
        self,
        dataset: DatasetInput,
        profile: "SourceProfile",
        table_index: dict[str, str],
        inferred_fk_by_name: dict[str, list["InferredFk"]],
    ) -> list[str]:
        """桥表引用的对象候选名（保持列出现序，供 select 端点选择）。

        顺序：父表（parenttype 样例解析，作首选端点/source）→ 声明式外键 → 推断外键。
        """
        ref_targets: list[str] = []
        parent_table = profile.resolve_parent_table(dataset, table_index)
        if parent_table:
            ref_targets.append(_infer_object_name(parent_table))
        for field in dataset.fields:
            if field.is_foreign_key and field.foreign_key_target:
                ref_targets.append(
                    _infer_object_name(field.foreign_key_target.split(".")[0])
                )
        for edge in inferred_fk_by_name.get(dataset.name, []):
            ref_targets.append(_infer_object_name(edge.target_table))
        return ref_targets

    def _reclassify_bridge_to_object(
        self,
        pack: ObjectTypeEvidencePack,
        role_by_object: dict[str, str],
        field_signals_by_name: dict[str, list[FieldSignal]],
        reeval_args_by_name: dict[str, dict],
        *,
        is_child_table: bool = False,
    ) -> None:
        """把「连不到两个业务对象、因而不是关系」的桥表智能重判为对象。

        抑制把它推向 bridge 的信号（is_child_table / fact / weak_fact）后重跑分类器，
        得到它作为**对象**时的角色：达标业务环节→业务对象，否则→数据表（分类器内的
        segment_size 降级已处理）。分类器若仍判 bridge（如多外键主键的关联式结构），
        兜底落数据表——它不是业务关系。全部标 needs_review。

        **例外：明细/子表不参与重判。** 子表锚点（Frappe 的 parent/parenttype 列）是
        从源 schema 里读出来的**事实**，不是推断出来的倾向。塌缩失败只说明「这条关系
        我们表达不出来」，不说明这张明细表变成了一个独立业务对象；把硬事实和软信号
        一起抑制掉重跑，「单列业务主键 +2.0」会独自把明细行顶成业务对象——实测 erpnext
        上 227 张子表因此被判成业务对象、与 LLM 的语义判定全面冲突，全部涌进人工队列。
        故子表直接落数据表（父表的明细），保留 is_child_table 证据，不再重跑分类器。
        人工挂载业务术语者豁免（人已认定它是业务概念）。
        """
        from app.services.object_classifier import ROLE_BRIDGE, ROLE_DATA_TABLE

        name = pack.candidate_name
        if is_child_table and not (reeval_args_by_name[name].get("glossary_terms") or []):
            pack.table_role = ROLE_DATA_TABLE
            pack.role_confidence = 0.6
            pack.role_reason = (
                "明细/子表（含 parent/parenttype 子表锚点，源 schema 事实）：未能塌缩为"
                "业务关系（连不到两个业务对象），落数据表——它是父表的明细行，"
                "不是独立业务对象，真正的业务对象落在其引用的键上"
            )
            pack.needs_review = True
            pack.role_signals = {
                "score": 0.0,
                "needs_review": True,
                "role": ROLE_DATA_TABLE,
                "signals": {"is_child_table": True},
                "reclassified_from": "bridge",
            }
            role_by_object[name] = ROLE_DATA_TABLE
            return
        res = classify_object_role(
            field_signals_by_name[name],
            **reeval_args_by_name[name],
            is_child_table=False,
            fact_name_token=None,
            weak_fact_name_token=None,
        )
        new_role = res.role if res.role != ROLE_BRIDGE else ROLE_DATA_TABLE
        label = {"business_object": "业务对象", "data_table": "数据表", "technical": "技术表"}.get(
            new_role, new_role
        )
        pack.table_role = new_role
        pack.role_reason = (
            f"关系表未能塌缩为业务关系（连不到两个业务对象），"
            f"智能重判为{label}：{res.reason}"
        )
        pack.needs_review = True
        pack.role_signals = {
            "score": res.score,
            "needs_review": True,
            "role": new_role,
            "signals": res.signals,
            "reclassified_from": "bridge",
        }
        role_by_object[name] = new_role

    def _collapse_and_reclassify_bridges(
        self,
        bundle: DataHubDomainBundle,
        *,
        object_types: list[ObjectTypeEvidencePack],
        profile: "SourceProfile",
        table_index: dict[str, str],
        inferred_fk_by_name: dict[str, list["InferredFk"]],
        fk_in_degree: dict[str, int],
        row_count_by_object: dict[str, int | None],
        field_signals_by_name: dict[str, list[FieldSignal]],
        reeval_args_by_name: dict[str, dict],
        child_table_by_name: dict[str, bool],
    ) -> list[RelationEvidencePack]:
        """桥表塌缩 + 智能重判，迭代到稳定。

        - 能连接两个业务对象的桥表 → 塌缩为一条 BO_A→BO_B 关系（桥表作 mapping 实现表）。
        - 连不到两个业务对象的桥表 → 本就不是关系 → 智能重判为对象（业务对象/数据表）。
        - 重判可能把某桥表提升为业务对象，从而让引用它的另一桥表凑出两个业务对象端点，
          故迭代：每轮先跳过还能靠「待定桥表引用」翻盘的，只重判确定无望的；收敛后再产出关系。
        """
        pack_by_name = {ot.candidate_name: ot for ot in object_types}
        role_by_object = {ot.candidate_name: ot.table_role for ot in object_types}
        label_by_object = {ot.candidate_name: ot.display_name for ot in object_types}
        in_degree_by_object: dict[str, int] = {}
        for tech_name, deg in fk_in_degree.items():
            obj = _infer_object_name(tech_name)
            in_degree_by_object[obj] = max(in_degree_by_object.get(obj, 0), deg)

        ds_by_bridge: dict[str, DatasetInput] = {}
        refs_by_bridge: dict[str, list[str]] = {}
        for dataset in bundle.datasets:
            name = _infer_object_name(dataset.name)
            if role_by_object.get(name) == "bridge":
                ds_by_bridge[name] = dataset
                refs_by_bridge[name] = self._bridge_ref_targets(
                    dataset, profile, table_index, inferred_fk_by_name
                )

        def endpoints_for(name: str):
            return select_bridge_endpoints(
                refs_by_bridge[name],
                role_by_object,
                in_degree_by_object,
                row_count_by_object,
                self_name=name,
            )

        pending = set(ds_by_bridge)
        for _ in range(_MAX_RECLASSIFY_ROUNDS):
            changed = False
            for name in list(pending):
                if endpoints_for(name) is not None:
                    continue  # 能塌缩 → 保持 bridge（是关系），最终统一产出
                refs = refs_by_bridge[name]
                # 还有翻盘希望：引用里「业务对象 + 仍待定的桥表」去重后 ≥2
                hopeful = {
                    r
                    for r in refs
                    if r != name
                    and (
                        role_by_object.get(r) == ROLE_BUSINESS_OBJECT
                        or r in pending
                    )
                }
                if len(hopeful) >= 2:
                    continue  # 推迟：待定桥表引用可能被提升为业务对象
                self._reclassify_bridge_to_object(
                    pack_by_name[name],
                    role_by_object,
                    field_signals_by_name,
                    reeval_args_by_name,
                    is_child_table=child_table_by_name.get(name, False),
                )
                pending.discard(name)
                changed = True
            if not changed:
                break
        # 剩余待定（桥表间循环引用）且仍塌不动 → 一律重判为对象
        for name in list(pending):
            if endpoints_for(name) is None:
                self._reclassify_bridge_to_object(
                    pack_by_name[name],
                    role_by_object,
                    field_signals_by_name,
                    reeval_args_by_name,
                    is_child_table=child_table_by_name.get(name, False),
                )
                pending.discard(name)

        # 产出：仍是 bridge 且能塌缩的桥表 → 一条塌缩关系。
        collapsed: list[RelationEvidencePack] = []
        for name, dataset in ds_by_bridge.items():
            if role_by_object.get(name) != "bridge":
                continue
            endpoints = endpoints_for(name)
            if endpoints is None:
                continue
            source, target = endpoints
            bridge_label = dataset.display_name or dataset.name
            source_label = label_by_object.get(source, source)
            target_label = label_by_object.get(target, target)
            collapsed.append(
                RelationEvidencePack(
                    name=name,  # 一桥一关系，用桥表候选名作稳定 upsert 键
                    # 关系语义词须为动词：桥表名是名词，故用通用关联动词「关联」，
                    # 具体承载表(桥表)由 mapping_object 记录、描述里给出。
                    display_name="关联",
                    source_object=source,
                    target_object=target,
                    cardinality="many_to_many",
                    structure_type="bridge_table",
                    description=(
                        f"{source_label} 与 {target_label} 通过关系表 {bridge_label} 关联"
                        "（桥表塌缩，待复核）"
                    ),
                    confidence=0.5,
                    evidence_refs=[dataset.urn],
                    mapping_object=name,
                )
            )
        return collapsed

    def _build_topology(
        self,
        bundle: DataHubDomainBundle,
        inferred_fk_by_name: dict[str, list["InferredFk"]] | None = None,
    ) -> tuple[
        dict[str, int],
        dict[str, int],
        dict[str, int],
        dict[str, int],
        dict[str, int],
    ]:
        """聚合跨表拓扑信号（不依赖表名含义）：

        - fk_in_degree: 按表名计数“有多少张不同的表通过外键指向它”。
        - lineage_up / lineage_down: 按 URN 计数血缘上/下游数量。
        - fk_out_degree: 按表名计数“这张表的外键指向多少张**不同**的目标表”
          （facet B：识别引用多个不同实体的关系/事实表）。
        - segment_size: 按表名给出“该表所属业务环节（关系图社区）的成员数”。业务对象
          判定的必要条件——只有隶属于 >= _MIN_SEGMENT_SIZE 成员聚类的表才可能是业务
          对象。在关系图（外键 + 推断外键 + 血缘，仅计域内两端）上跑社区检测（标签传播，
          不做枢纽摘除，让高连接的主数据自然落入大簇）得到，孤立表/孤对成员数 < 阈值。

        外键既包含声明式 `is_foreign_key`，也包含源画像推断的边（如 Frappe Link 字段），
        两者合并后统一参与入度/出度统计。
        """
        inferred_fk_by_name = inferred_fk_by_name or {}
        names = {ds.name for ds in bundle.datasets}
        name_by_urn = {ds.urn: ds.name for ds in bundle.datasets}
        # 业务环节判定用的无向关系图：节点为域内表名，边取域内两端的外键/血缘关联。
        adjacency: dict[str, set[str]] = {name: set() for name in names}

        def _link(a: str, b: str) -> None:
            if a != b and a in adjacency and b in adjacency:
                adjacency[a].add(b)
                adjacency[b].add(a)

        fk_in_degree: dict[str, set[str]] = {}
        fk_out_targets: dict[str, set[str]] = {}
        for ds in bundle.datasets:
            for f in ds.fields:
                if f.is_foreign_key and f.foreign_key_target:
                    target_table = f.foreign_key_target.split(".")[0]
                    fk_in_degree.setdefault(target_table, set()).add(ds.name)
                    fk_out_targets.setdefault(ds.name, set()).add(target_table)
                    _link(ds.name, target_table)
            for edge in inferred_fk_by_name.get(ds.name, []):
                if edge.target_table == ds.name:
                    continue
                fk_in_degree.setdefault(edge.target_table, set()).add(ds.name)
                fk_out_targets.setdefault(ds.name, set()).add(edge.target_table)
                _link(ds.name, edge.target_table)
        fk_counts = {table: len(refs) for table, refs in fk_in_degree.items()}
        fk_out_counts = {table: len(tgts) for table, tgts in fk_out_targets.items()}

        lineage_up: dict[str, int] = {}
        lineage_down: dict[str, int] = {}
        for lin in bundle.lineages:
            lineage_down[lin.source_urn] = lineage_down.get(lin.source_urn, 0) + 1
            lineage_up[lin.target_urn] = lineage_up.get(lin.target_urn, 0) + 1
            src = name_by_urn.get(lin.source_urn)
            tgt = name_by_urn.get(lin.target_urn)
            if src and tgt:
                _link(src, tgt)

        # 关系图社区检测 → 每张表所属业务环节的成员数（孤立表自成 1 成员簇）。
        clusters = label_propagation_clusters(sorted(names), adjacency)
        segment_size = {
            name: len(cluster) for cluster in clusters for name in cluster
        }
        return fk_counts, lineage_up, lineage_down, fk_out_counts, segment_size

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

    def _refine_business_relations(
        self,
        relations: list[RelationEvidencePack],
        role_by_object: dict[str, str],
        label_by_object: dict[str, str],
        row_count_by_object: dict[str, int | None],
    ) -> list[RelationEvidencePack]:
        """精炼关系，使「业务关系只存在于业务对象之间」，并消除无意义的「派生出」。

        规则（对齐产品裁决）：
        1. rule 1：任何业务关联关系（外键/桥/事实）两端都必须是业务对象，否则丢弃。
        2. 血缘去重：若某对象对已存在业务关联结构（FK/桥/事实），其上的血缘边只是
           重复表达同一关联，丢弃。
        3. 血缘 rule 1：任一端非业务对象的血缘丢弃（溯源到系统/数据表不进业务关系图）。
        4. 反向引用翻转：仍为兜底「派生出」且两端均业务对象的血缘，实为「单据引用主数据」
           而非派生——按行数/关联度判主/明细，翻转为 明细→主数据 的引用关系
           （structure_type=foreign_key），命名为「位于/属于/采用/引用」。判不出主/明细
           不对称时，诚实保留「派生出」。
        """
        if not relations:
            return relations

        def is_bo(obj: str) -> bool:
            return role_by_object.get(obj) == ROLE_BUSINESS_OBJECT

        business_struct = {"foreign_key", "bridge_table", "fact_table"}
        fk_pairs: set[frozenset[str]] = {
            frozenset((r.source_object, r.target_object))
            for r in relations
            if r.structure_type in business_struct
        }

        # 无向关联度：主数据/维度被更多表关联，用作翻转定向的次级依据。
        neighbors: dict[str, set[str]] = {}
        for r in relations:
            neighbors.setdefault(r.source_object, set()).add(r.target_object)
            neighbors.setdefault(r.target_object, set()).add(r.source_object)
        degree = {obj: len(peers) for obj, peers in neighbors.items()}

        kept: list[RelationEvidencePack] = []
        for rel in relations:
            both_bo = is_bo(rel.source_object) and is_bo(rel.target_object)
            if rel.structure_type != "derivation":
                if both_bo:  # rule 1
                    kept.append(rel)
                continue

            pair = frozenset((rel.source_object, rel.target_object))
            if pair in fk_pairs:  # 2) 与已有业务关联重复
                continue
            if not both_bo:  # 3) rule 1
                continue
            if rel.display_name != "派生出":  # 已具体命名（转化/包含/加工类）→ 保留溯源
                kept.append(rel)
                continue
            # 4) 反向引用翻转（判不出主/明细时原样保留「派生出」）。
            kept.append(
                self._orient_reference(
                    rel, label_by_object, row_count_by_object, degree
                )
            )
        return kept

    @staticmethod
    def _orient_reference(
        rel: RelationEvidencePack,
        label_by_object: dict[str, str],
        row_count_by_object: dict[str, int | None],
        degree: dict[str, int],
    ) -> RelationEvidencePack:
        """把兜底「派生出」的血缘翻转为「单据(多行/低关联) 引用 主数据(少行/高关联)」。

        主/明细不对称判据：先看行数（明细行多、主数据行少），行数无差异时退回关联度
        （主数据被更多表关联）。都判不出时返回原关系（保留「派生出」）。
        """
        a, b = rel.source_object, rel.target_object
        ra, rb = row_count_by_object.get(a), row_count_by_object.get(b)
        detail: str | None = None
        master: str | None = None
        if ra is not None and rb is not None and ra != rb:
            detail, master = (a, b) if ra > rb else (b, a)
        else:
            da, db = degree.get(a, 0), degree.get(b, 0)
            if da != db:
                detail, master = (a, b) if da < db else (b, a)
        if detail is None or master is None:
            return rel  # 无法判别主/明细，诚实保留「派生出」

        detail_label = label_by_object.get(detail, detail)
        master_label = label_by_object.get(master, master)
        return rel.model_copy(
            update={
                "name": f"{detail}_to_{master}",
                "display_name": reference_term(master_label),
                "source_object": detail,
                "target_object": master,
                "cardinality": "many_to_one",
                "structure_type": "foreign_key",
                "description": (
                    f"{detail_label} 引用主数据 {master_label}"
                    "（由血缘方向翻转推断，待复核）"
                ),
                "confidence": min(rel.confidence, 0.5),
            }
        )

    def _infer_semantic_type(self, field) -> str:
        guess = self._guess_semantic_type(field)
        # **名字是线索，物理类型与样例是事实**。物理类型是文本、而名字猜出的语义会把
        # 目标列定成 TIMESTAMP/DECIMAL/BOOLEAN 时，除非样例值确实长那样，否则退回
        # attribute——ERP 里 date_format(VARCHAR,"dd-mm-yyyy")、_user_tags(TEXT,'["a"]')
        # 正是这么被判成 datetime / flag 的，物化出来的表装不下自己的源数据。
        if guess in _PHYSICAL_SEMANTICS and _is_text_physical(
            getattr(field, "data_type", None)
        ):
            if not _samples_support(guess, getattr(field, "sample_values", None)):
                return "attribute"
        return guess

    @staticmethod
    def _guess_semantic_type(field) -> str:
        """只看字段名的那一层猜测。与物理类型的对账在 _infer_semantic_type 里做。"""
        name = field.name.lower()
        if field.is_primary_key or name.endswith("_id"):
            return "identifier"
        # 技术词汇字段（token/secret/hash/config/session 等）：内容信号，不看表名，
        # 供分类器识别技术/系统表。放在 datetime/amount 之前，避免 expires_at
        # 落到 datetime、避免其他技术字段落到默认 attribute。
        if _is_technical_field(name):
            return "technical"
        if (
            "date" in name
            or "time" in name
            or name.endswith(("_at", "_ts", "_dt"))
            or name in ("created", "updated", "modified", "ctime", "mtime")
        ):
            return "datetime"
        if "amount" in name or "price" in name:
            return "amount"
        if "status" in name or "type" in name or "level" in name:
            return "category"
        if "tag" in name or name.startswith("is_"):
            return "flag"
        return "attribute"



def scope_evidence(
    evidence: EvidenceBundle, dataset_urns: list[str] | None
) -> EvidenceBundle:
    """把证据包裁剪到指定的数据集，供「只对这几张表跑生成」使用。

    ``dataset_urns`` 为空/None 时**原样返回**——全域生成是默认行为，不因这个入口改变。

    裁剪是过滤而非重建：``object_types`` 按 ``source_dataset_urn`` 命中，``properties``
    与 ``relations`` 跟着存活的 ``candidate_name`` 走。这样裁剪后的包与全域包在结构上
    完全同形，下游生成/合并不需要知道自己拿到的是不是子集。

    为什么必须裁剪证据、而不是生成完再筛结果：LLM 成本正比于喂进去的表数（ERP 域 734
    张表一次几十万 token），全跑一遍再丢掉才是要消除的那件事。合并侧本就按
    ``source_ref`` upsert 且 ``handle_removal=False``，子集不会误删域内其它对象。
    """
    if not dataset_urns:
        return evidence
    wanted = {urn for urn in dataset_urns if urn}
    objects = [o for o in evidence.object_types if o.source_dataset_urn in wanted]
    names = {o.candidate_name for o in objects}
    return EvidenceBundle(
        object_types=objects,
        properties=[p for p in evidence.properties if p.object_candidate_name in names],
        # 两端都在子集内才留：只留半条边会让关系指向一个本次并不生成的对象。
        relations=[
            r
            for r in evidence.relations
            if r.source_object in names and r.target_object in names
        ],
        business_logics=list(evidence.business_logics),
    )
