import asyncio
import logging
import random

import httpx

from app.config import settings
from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    DomainInput,
    FieldInput,
    LineageInput,
    LogicEvidenceInput,
)

logger = logging.getLogger("ontometa.datahub")

# GraphQL 请求对**瞬时**网络故障重试（如 ngrok/反代断连、超时、5xx）。仅限确定
# 可安全重放的错误；GraphQL 业务错误(payload.errors)与 4xx 不重试。
_MAX_GRAPHQL_ATTEMPTS = 3
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.RemoteProtocolError,  # "Server disconnected without sending a response"
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_SAMPLE_VALUES_PER_FIELD = 5
_SAMPLE_VALUE_MAX_LENGTH = 60

_DATASET_ENTITY_FRAGMENT = """
fragment DatasetDetails on Dataset {
  urn
  name
  properties { description }
  platform { name }
  container { properties { name } }
  subTypes { typeNames }
  tags {
    tags {
      tag {
        urn
        ... on Tag { properties { name } }
      }
    }
  }
  glossaryTerms {
    terms {
      term {
        urn
        ... on GlossaryTerm { properties { name } }
      }
    }
  }
  schemaMetadata {
    fields { fieldPath type nativeDataType description }
    primaryKeys
    foreignKeys {
      name
      sourceFields { fieldPath }
      foreignFields { fieldPath }
      foreignDataset { urn name }
    }
  }
  datasetProfiles(limit: 1) {
    rowCount
    fieldProfiles {
      fieldPath
      sampleValues
      uniqueCount
    }
  }
  downstreamLineage: lineage(input: { direction: DOWNSTREAM, start: 0, count: 50 }) {
    relationships {
      entity { urn type ... on Dataset { name } }
    }
  }
  upstreamLineage: lineage(input: { direction: UPSTREAM, start: 0, count: 50 }) {
    relationships {
      entity { urn type ... on Dataset { name } }
    }
  }
}
"""

_ENTITY_BATCH_SIZE = 20
_DOMAIN_ENTITY_PAGE_SIZE = 100


def _field_path(field_path: str) -> str:
    return field_path.split(".")[-1] if field_path else field_path


def _parse_owner(raw: dict | None) -> str | None:
    if not raw:
        return None
    owners = raw.get("owners") or []
    for owner in owners:
        owner_entity = owner.get("owner") or {}
        props = owner_entity.get("properties") or {}
        display_name = props.get("displayName") or props.get("fullName")
        if display_name:
            return display_name
        if owner_entity.get("username"):
            return owner_entity["username"]
    return None


def _parse_domain(raw: dict) -> DomainInput:
    props = raw.get("properties") or {}
    return DomainInput(
        id=raw["urn"],
        name=props.get("name") or raw["urn"],
        description=props.get("description"),
        owner=_parse_owner(raw.get("ownership")),
    )


def _parse_field_profiles(raw: dict) -> dict[str, dict]:
    """从最近一次 datasetProfiles 提取 {字段名: {sample_values, unique_count}}。

    未开启 profiling 或尚无采集结果时 datasetProfiles/fieldProfiles 为空，
    优雅降级为空字典，不影响后续流程。
    """
    profiles = raw.get("datasetProfiles") or []
    if not profiles:
        return {}
    field_profiles = profiles[0].get("fieldProfiles") or []
    result: dict[str, dict] = {}
    for fp in field_profiles:
        name = _field_path(fp.get("fieldPath", ""))
        if not name:
            continue
        values = fp.get("sampleValues") or []
        truncated = [str(v)[:_SAMPLE_VALUE_MAX_LENGTH] for v in values[:_SAMPLE_VALUES_PER_FIELD]]
        unique_count = fp.get("uniqueCount")
        result[name] = {
            "sample_values": truncated,
            "unique_count": unique_count if isinstance(unique_count, int) else None,
        }
    return result


def _parse_row_count(raw: dict) -> int | None:
    """从最近一次 datasetProfiles 提取总行数（未采集时为 None）。"""
    profiles = raw.get("datasetProfiles") or []
    if not profiles:
        return None
    row_count = profiles[0].get("rowCount")
    return row_count if isinstance(row_count, int) else None


def _parse_subtypes(raw: dict) -> list[str]:
    """提取 DataHub subType 名称（如 View/Table），供角色分类器识别。"""
    container = raw.get("subTypes") or {}
    names = container.get("typeNames") or []
    return [str(n) for n in names if n]


def _parse_tags(raw: dict) -> list[str]:
    """提取数据集上挂载的 tag 名称（去重、保序）。"""
    container = raw.get("tags") or {}
    entries = container.get("tags") or []
    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        tag = (entry or {}).get("tag") or {}
        props = tag.get("properties") or {}
        name = props.get("name") or tag.get("urn")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _parse_glossary_terms(raw: dict) -> list[str]:
    """提取数据集上人工挂载的业务术语名称（去重、保序）。"""
    container = raw.get("glossaryTerms") or {}
    terms = container.get("terms") or []
    names: list[str] = []
    seen: set[str] = set()
    for entry in terms:
        term = (entry or {}).get("term") or {}
        props = term.get("properties") or {}
        name = props.get("name") or term.get("urn")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _parse_schema_fields(
    schema_metadata: dict | None, field_profiles: dict[str, dict] | None = None
) -> list[FieldInput]:
    if not schema_metadata:
        return []
    field_profiles = field_profiles or {}

    primary_keys = set(schema_metadata.get("primaryKeys") or [])
    foreign_key_by_source: dict[str, tuple[str, str]] = {}
    for fk in schema_metadata.get("foreignKeys") or []:
        source_fields = fk.get("sourceFields") or []
        foreign_fields = fk.get("foreignFields") or []
        foreign_dataset = fk.get("foreignDataset") or {}
        target_table = foreign_dataset.get("name") or _extract_dataset_name(foreign_dataset.get("urn", ""))
        for idx, source_field in enumerate(source_fields):
            source_name = _field_path(source_field.get("fieldPath", ""))
            foreign_name = _field_path(
                foreign_fields[idx].get("fieldPath", "") if idx < len(foreign_fields) else source_name
            )
            if source_name and target_table:
                foreign_key_by_source[source_name] = (target_table, foreign_name)

    fields: list[FieldInput] = []
    for field in schema_metadata.get("fields") or []:
        name = _field_path(field.get("fieldPath", ""))
        if not name:
            continue
        fk_target = None
        is_foreign_key = False
        if name in foreign_key_by_source:
            is_foreign_key = True
            target_table, target_field = foreign_key_by_source[name]
            fk_target = f"{target_table}.{target_field}"
        profile = field_profiles.get(name) or {}
        fields.append(
            FieldInput(
                name=name,
                display_name=field.get("description") or name,
                description=field.get("description"),
                data_type=field.get("nativeDataType") or field.get("type"),
                is_primary_key=name in primary_keys,
                is_foreign_key=is_foreign_key,
                foreign_key_target=fk_target,
                sample_values=profile.get("sample_values", []),
                unique_count=profile.get("unique_count"),
            )
        )
    return fields


def _extract_dataset_name(urn: str) -> str:
    if not urn:
        return urn
    if urn.startswith("urn:li:dataset:"):
        inner = urn[len("urn:li:dataset:") :]
        if inner.startswith("(") and inner.endswith(")"):
            parts = inner[1:-1].split(",")
            if len(parts) >= 2:
                return parts[-2]
    return urn


def _extract_platform(urn: str) -> str | None:
    """从 dataset URN 取源平台，如 ``mariadb``。

    URN 形如 ``urn:li:dataset:(urn:li:dataPlatform:mariadb,db.tab,PROD)``。
    平台决定搬运作业用哪个连接器（见 ``app/warehouse/jobs/``），取不到即返回 None，
    由调用方显式处理，不猜一个默认值。
    """
    if not urn or not urn.startswith("urn:li:dataset:"):
        return None
    inner = urn[len("urn:li:dataset:") :]
    if not (inner.startswith("(") and inner.endswith(")")):
        return None
    head = inner[1:-1].split(",")[0]
    prefix = "urn:li:dataPlatform:"
    return head[len(prefix) :] or None if head.startswith(prefix) else None


def build_dataset_urn(platform: str, name: str, fabric: str = "PROD") -> str:
    """构造 Dataset URN，与 ``_extract_dataset_name`` / ``_extract_platform`` 互逆。

    ``name`` 为 ``库.表``（或无库时的表名），``fabric`` 为 DataHub 环境标
    （PROD/DEV/…）。目标表 URN 需要它才能确定——``JobSpec`` 刻意不构造目标 URN
    正是因为 fabric 属于部署环境，编译期无从得知。此函数是**唯一**的目标 URN 构造点，
    血缘上报（兜底 emitter）与 DAG outlets 注入都走它，保证两条路径产同一份 URN。
    """
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{fabric})"


def _extract_container_name(raw: dict) -> str | None:
    container = raw.get("container") or {}
    props = container.get("properties") or {}
    if props.get("name"):
        return props["name"]
    urn = raw.get("urn", "")
    table_ref = _extract_dataset_name(urn)
    if "." in table_ref:
        return table_ref.split(".", 1)[0]
    return None


def _parse_dataset_entity(raw: dict) -> DatasetInput:
    props = raw.get("properties") or {}
    platform = (raw.get("platform") or {}).get("name")
    container = _extract_container_name(raw)
    name = raw.get("name") or _extract_dataset_name(raw.get("urn", ""))
    return DatasetInput(
        urn=raw["urn"],
        name=name,
        display_name=name,
        description=props.get("description"),
        platform=platform,
        container=container,
        fields=_parse_schema_fields(
            raw.get("schemaMetadata"), _parse_field_profiles(raw)
        ),
        row_count=_parse_row_count(raw),
        glossary_terms=_parse_glossary_terms(raw),
        subtypes=_parse_subtypes(raw),
        tags=_parse_tags(raw),
    )


def _parse_lineages_from_entities(
    entities: list[dict],
    domain_dataset_urns: set[str],
) -> list[LineageInput]:
    seen: set[tuple[str, str]] = set()
    lineages: list[LineageInput] = []
    for entity in entities:
        current_urn = entity.get("urn")
        if not current_urn or current_urn not in domain_dataset_urns:
            continue

        # DOWNSTREAM: current_urn -> target_urn (current feeds into downstream)
        downstream = entity.get("downstreamLineage") or {}
        for rel in downstream.get("relationships") or []:
            target_entity = rel.get("entity") or {}
            target_urn = target_entity.get("urn")
            if not target_urn or target_entity.get("type") != "DATASET":
                continue
            if target_urn not in domain_dataset_urns:
                continue
            key = (current_urn, target_urn)
            if key in seen:
                continue
            seen.add(key)
            lineages.append(LineageInput(source_urn=current_urn, target_urn=target_urn))

        # UPSTREAM: upstream_urn -> current_urn (upstream feeds into current)
        upstream = entity.get("upstreamLineage") or {}
        for rel in upstream.get("relationships") or []:
            upstream_entity = rel.get("entity") or {}
            upstream_urn = upstream_entity.get("urn")
            if not upstream_urn or upstream_entity.get("type") != "DATASET":
                continue
            if upstream_urn not in domain_dataset_urns:
                continue
            key = (upstream_urn, current_urn)
            if key in seen:
                continue
            seen.add(key)
            lineages.append(LineageInput(source_urn=upstream_urn, target_urn=current_urn))

    return lineages


def _parse_lineage_edges_from_entities(entities: list[dict]) -> list[LineageInput]:
    """从单个/少量 dataset 实体里提取全部血缘边（**不受 domain 过滤**）。

    与 :func:`_parse_lineages_from_entities` 的区别：后者只保留两端都在目标域内的边
    （建模时收敛到域边界）；本函数返回查询到的**全部**边——L2 的用途是看某个表
    的上下游（包括跨域/外部表），故不过滤。

    边方向与 DataHub 一致：downstreamLineage = 本表 → 下游表（本表被下游消费），
    upstreamLineage = 上游表 → 本表（本表消费上游）。
    """
    seen: set[tuple[str, str]] = set()
    edges: list[LineageInput] = []
    for entity in entities:
        current_urn = entity.get("urn")
        if not current_urn:
            continue

        downstream = entity.get("downstreamLineage") or {}
        for rel in downstream.get("relationships") or []:
            target_entity = rel.get("entity") or {}
            target_urn = target_entity.get("urn")
            if not target_urn or target_entity.get("type") != "DATASET":
                continue
            key = (current_urn, target_urn)
            if key not in seen:
                seen.add(key)
                edges.append(
                    LineageInput(source_urn=current_urn, target_urn=target_urn)
                )

        upstream = entity.get("upstreamLineage") or {}
        for rel in upstream.get("relationships") or []:
            upstream_entity = rel.get("entity") or {}
            upstream_urn = upstream_entity.get("urn")
            if not upstream_urn or upstream_entity.get("type") != "DATASET":
                continue
            key = (upstream_urn, current_urn)
            if key not in seen:
                seen.add(key)
                edges.append(
                    LineageInput(source_urn=upstream_urn, target_urn=current_urn)
                )

    return edges
    source_ref = f"{dataset.container}.{dataset.name}" if dataset.container else dataset.name
    evidences: list[LogicEvidenceInput] = []
    for query in queries:
        props = query.get("properties") or {}
        statement = props.get("statement") or {}
        expression = statement.get("value")
        if not expression:
            continue
        if "information_schema" in source_ref or "CREATE ALGORITHM" in expression[:200]:
            continue
        name = props.get("name") or query.get("urn", "query")
        evidences.append(
            LogicEvidenceInput(
                source_type=(statement.get("language") or "sql").lower(),
                source_ref=source_ref,
                name=name,
                expression=expression[:2000],
                description=props.get("description"),
            )
        )
    return evidences


def _parse_search_dataset(item: dict) -> DatasetInput:
    entity = item.get("entity") or item
    props = entity.get("properties") or {}
    platform = (entity.get("platform") or {}).get("name")
    name = entity.get("name") or _extract_dataset_name(entity.get("urn", ""))
    return DatasetInput(
        urn=entity["urn"],
        name=name,
        display_name=name,
        description=props.get("description"),
        platform=platform,
        container=_extract_container_name(entity),
    )


class DataHubConnector:
    """对接 DataHub GraphQL API，输出统一内部输入模型。"""

    def __init__(self, runtime_config=None) -> None:
        from app.config import settings as env_settings

        if runtime_config is None:
            self.api_url = env_settings.datahub_gms_url.rstrip("/")
            self.frontend_url = env_settings.datahub_frontend_url.rstrip("/")
            self.token = env_settings.datahub_token
        else:
            self.api_url = runtime_config.gms_url.rstrip("/")
            self.frontend_url = runtime_config.frontend_url.rstrip("/")
            self.token = runtime_config.token

        # 懒初始化的复用 httpx 客户端，连接池跨多次 GraphQL 请求复用。
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                trust_env=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def aclose(self) -> None:
        """显式释放底层 httpx 连接池。调用方在请求结束时调用。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_domains(self) -> list[DomainInput]:
        return await self._fetch_domains_from_api()

    async def fetch_domain_bundle(
        self,
        datahub_domain_id: str,
        *,
        include_logic_evidences: bool = False,
    ) -> DataHubDomainBundle:
        return await self._fetch_domain_bundle_from_api(
            datahub_domain_id,
            include_logic_evidences=include_logic_evidences,
        )

    async def get_dataset_by_urn(self, dataset_urn: str) -> DatasetInput:
        entities = await self._fetch_dataset_entities([dataset_urn])
        if not entities:
            raise ValueError(f"DataHub dataset not found: {dataset_urn}")
        return _parse_dataset_entity(entities[0])

    async def get_lineage_around(
        self,
        dataset_urn: str,
        *,
        direction: str = "BOTH",
        depth: int = 1,
        count: int = 50,
    ) -> dict:
        """L2：实时查单个数据集的血缘（上游/下游），返回邻域边。

        **DataHub 是血缘的唯一权威**——本方法直接查 DataHub，不做本地推导。
        与建模期的 :meth:`fetch_domain_bundle` 不同，这里只查一个 URN 的邻域，
        且**不过滤 domain**：要看的是某个表的全部上下游（含跨域/外部表）。

        Args:
            dataset_urn: 中心数据集 URN。
            direction: UPSTREAM / DOWNSTREAM / BOTH。
            depth: 展开深度（1 = 直接上下游；>1 逐层展开）。
            count: 每层最多返回的关系数。

        Returns:
            ``{"center_urn", "edges": [LineageInput...], "nodes": [ {urn, name, platform}... ]}``
            查不到或网络失败时 edges 为空（**不抛错**——血缘是增强，缺失不该阻断调用方）。
        """
        if direction not in ("UPSTREAM", "DOWNSTREAM", "BOTH"):
            raise ValueError(f"direction 须为 UPSTREAM/DOWNSTREAM/BOTH，收到「{direction}」")

        query = """
        fragment DatasetLineageFragment on Dataset {
          urn
          name
          platform { name }
          downstreamLineage: lineage(input: { direction: DOWNSTREAM, start: 0, count: $count }) {
            relationships {
              entity { urn type ... on Dataset { name platform { name } } }
            }
          }
          upstreamLineage: lineage(input: { direction: UPSTREAM, start: 0, count: $count }) {
            relationships {
              entity { urn type ... on Dataset { name platform { name } } }
            }
          }
        }
        query getLineageAround($urn: String!, $count: Int!) {
          dataset(urn: $urn) {
            ...DatasetLineageFragment
          }
        }
        """
        try:
            data = await self._graphql(query, {"urn": dataset_urn, "count": count})
        except Exception as exc:  # noqa: BLE001 —— 血缘是增强，读不到不炸调用方
            logger.warning(
                "datahub get_lineage_around(%s) failed: %s", dataset_urn, exc
            )
            return {"center_urn": dataset_urn, "edges": [], "nodes": []}

        dataset = data.get("dataset")
        if not dataset:
            return {"center_urn": dataset_urn, "edges": [], "nodes": []}

        edges = _parse_lineage_edges_from_entities([dataset])

        # 按方向过滤（GraphQL 已按方向查，这里兜底双保险）
        if direction == "UPSTREAM":
            edges = [e for e in edges if e.target_urn == dataset_urn]
        elif direction == "DOWNSTREAM":
            edges = [e for e in edges if e.source_urn == dataset_urn]

        # 邻域节点（中心 + 边端点），去重保序
        nodes: dict[str, dict] = {dataset_urn: {"urn": dataset_urn}}
        for e in edges:
            nodes.setdefault(e.source_urn, {"urn": e.source_urn})
            nodes.setdefault(e.target_urn, {"urn": e.target_urn})

        return {
            "center_urn": dataset_urn,
            "edges": [e.model_dump() for e in edges],
            "nodes": list(nodes.values()),
        }

    def get_domain_url(self, datahub_domain_id: str) -> str:
        return f"{self.frontend_url}/domain/{datahub_domain_id}"

    def get_dataset_url(self, dataset_ref: str) -> str:
        from urllib.parse import quote

        urn = dataset_ref
        if not dataset_ref.startswith("urn:"):
            urn = f"urn:li:dataset:(urn:li:dataPlatform:hive,{dataset_ref},PROD)"
        return f"{self.frontend_url}/dataset/{quote(urn, safe='')}"

    async def search_datasets(self, query: str = "") -> list[DatasetInput]:
        return await self._search_datasets_from_api(query)

    async def _search_datasets_from_api(self, query: str) -> list[DatasetInput]:
        # DataHub 会对 search 默认套上 schemaField→dataset 的分组 searchFlags；一旦
        # query 为空串，Elasticsearch 侧会因空查询 + 分组而 500（"Search query failed"）。
        # "*" 是 DataHub 规范的“全量匹配”写法，既能列全部数据集又避开这条坏路径。
        keyword = query.strip() or "*"
        graphql_query = """
        query searchDatasets($keyword: String!) {
          search(input: { type: DATASET, query: $keyword, start: 0, count: 50 }) {
            searchResults {
              entity {
                urn
                ... on Dataset {
                  name
                  properties { description }
                  platform { name }
                }
              }
            }
          }
        }
        """
        data = await self._graphql(graphql_query, {"keyword": keyword})
        results = data.get("search", {}).get("searchResults", [])
        return [_parse_search_dataset(item) for item in results]

    async def _fetch_domains_from_api(self) -> list[DomainInput]:
        query = """
        query listDomains {
          listDomains(input: { start: 0, count: 100 }) {
            domains {
              urn
              properties { name description }
              ownership {
                owners {
                  owner {
                    ... on CorpUser {
                      username
                      properties { displayName fullName }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._graphql(query)
        domains = data.get("listDomains", {}).get("domains", [])
        return [_parse_domain(item) for item in domains]

    async def _fetch_domain_bundle_from_api(
        self,
        datahub_domain_id: str,
        *,
        include_logic_evidences: bool = False,
    ) -> DataHubDomainBundle:
        domain_raw = await self._fetch_domain_raw(datahub_domain_id)
        if not domain_raw:
            raise ValueError(f"Domain not found: {datahub_domain_id}")

        domain = _parse_domain(domain_raw)
        dataset_urns = await self._fetch_domain_dataset_urns(datahub_domain_id)
        dataset_entities = await self._fetch_dataset_entities(dataset_urns)
        datasets = [_parse_dataset_entity(item) for item in dataset_entities]

        domain_urn_set = set(dataset_urns)
        lineages = _parse_lineages_from_entities(dataset_entities, domain_urn_set)

        logic_evidences: list[LogicEvidenceInput] = []
        if include_logic_evidences:
            logic_evidences = await self._fetch_all_dataset_queries(datasets)

        return DataHubDomainBundle(
            domain=domain,
            datasets=datasets,
            lineages=lineages,
            logic_evidences=logic_evidences,
        )

    async def _fetch_domain_raw(self, datahub_domain_id: str) -> dict | None:
        query = """
        query getDomain($urn: String!) {
          domain(urn: $urn) {
            urn
            properties { name description }
            ownership {
              owners {
                owner {
                  ... on CorpUser {
                    username
                    properties { displayName fullName }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._graphql(query, {"urn": datahub_domain_id})
        return data.get("domain")

    async def _fetch_domain_dataset_urns(self, datahub_domain_id: str) -> list[str]:
        query = """
        query domainEntities($urn: String!, $start: Int!, $count: Int!) {
          domain(urn: $urn) {
            entities(input: { start: $start, count: $count }) {
              start
              count
              total
              searchResults {
                entity { urn type }
              }
            }
          }
        }
        """
        urns: list[str] = []
        start = 0
        while True:
            data = await self._graphql(
                query,
                {"urn": datahub_domain_id, "start": start, "count": _DOMAIN_ENTITY_PAGE_SIZE},
            )
            entities_page = (data.get("domain") or {}).get("entities") or {}
            results = entities_page.get("searchResults") or []
            for item in results:
                entity = item.get("entity") or {}
                if entity.get("type") == "DATASET" and entity.get("urn"):
                    urns.append(entity["urn"])
            total = entities_page.get("total", 0)
            start += entities_page.get("count", len(results))
            if start >= total or not results:
                break
        return urns

    async def _fetch_dataset_entities(self, urns: list[str]) -> list[dict]:
        if not urns:
            return []

        query = _DATASET_ENTITY_FRAGMENT + """
        query fetchDatasets($urns: [String!]!) {
          entities(urns: $urns) {
            ...DatasetDetails
          }
        }
        """
        batches = [
            urns[idx : idx + _ENTITY_BATCH_SIZE]
            for idx in range(0, len(urns), _ENTITY_BATCH_SIZE)
        ]
        semaphore = asyncio.Semaphore(settings.datahub_max_concurrency)

        async def fetch_batch(batch: list[str]) -> list[dict]:
            async with semaphore:
                data = await self._graphql(query, {"urns": batch})
                return data.get("entities") or []

        batch_results = await asyncio.gather(*(fetch_batch(batch) for batch in batches))
        entities: list[dict] = []
        for batch_entities in batch_results:
            entities.extend(batch_entities)
        return [item for item in entities if item.get("urn")]

    async def _fetch_all_dataset_queries(
        self, datasets: list[DatasetInput]
    ) -> list[LogicEvidenceInput]:
        if not datasets:
            return []

        semaphore = asyncio.Semaphore(settings.datahub_max_concurrency)

        async def fetch_for_dataset(dataset: DatasetInput) -> list[LogicEvidenceInput]:
            async with semaphore:
                queries = await self._fetch_dataset_queries(dataset.urn)
                return _parse_query_logic_evidences(dataset, queries)

        results = await asyncio.gather(*(fetch_for_dataset(ds) for ds in datasets))
        logic_evidences: list[LogicEvidenceInput] = []
        for items in results:
            logic_evidences.extend(items)
        return logic_evidences

    async def _fetch_dataset_queries(self, dataset_urn: str) -> list[dict]:
        query = """
        query listDatasetQueries($datasetUrn: String!, $start: Int!, $count: Int!) {
          listQueries(input: { datasetUrn: $datasetUrn, start: $start, count: $count }) {
            total
            queries {
              urn
              properties {
                name
                description
                source
                statement { value language }
              }
            }
          }
        }
        """
        queries: list[dict] = []
        start = 0
        page_size = 50
        while True:
            data = await self._graphql(
                query,
                {"datasetUrn": dataset_urn, "start": start, "count": page_size},
            )
            page = data.get("listQueries") or {}
            batch = page.get("queries") or []
            queries.extend(batch)
            total = page.get("total", 0)
            start += len(batch)
            if start >= total or not batch:
                break
        return queries

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        client = self._get_client()
        url = f"{self.api_url}/api/graphql"
        body = {"query": query, "variables": variables or {}}

        last_exc: Exception | None = None
        for attempt in range(_MAX_GRAPHQL_ATTEMPTS):
            try:
                response = await client.post(url, json=body, headers=headers)
                response.raise_for_status()
                payload = response.json()
                if "errors" in payload:
                    # GraphQL 业务错误：非瞬时，不重试。
                    raise RuntimeError(str(payload["errors"]))
                return payload.get("data", {})
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if (
                    exc.response.status_code in _RETRYABLE_STATUS
                    and attempt < _MAX_GRAPHQL_ATTEMPTS - 1
                ):
                    await self._backoff_sleep(attempt, exc)
                    continue
                raise
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                last_exc = exc
                if attempt < _MAX_GRAPHQL_ATTEMPTS - 1:
                    await self._backoff_sleep(attempt, exc)
                    continue
                raise
        # 理论不可达（最后一次尝试要么 return 要么 raise），兜底防御。
        if last_exc is not None:
            raise last_exc
        return {}

    @staticmethod
    async def _backoff_sleep(attempt: int, exc: Exception) -> None:
        """指数退避 + 抖动：0.5s、1s（+最多 0.3s 抖动）。"""
        delay = 0.5 * (2**attempt) + random.uniform(0, 0.3)
        logger.warning(
            "datahub graphql transient error (attempt %d/%d): %s — retrying in %.1fs",
            attempt + 1,
            _MAX_GRAPHQL_ATTEMPTS,
            type(exc).__name__,
            delay,
        )
        await asyncio.sleep(delay)


# ---------------------------------------------------------------- 回写（M7）
#
# 连接器此前为纯只读。以下 mutation 让本体成果（业务命名、描述、术语、域）
# 回灌 DataHub，形成元数据双向闭环。
#
# 结构已对照 DataHub 开源 GraphQL schema 核实一致（datahub-graphql-core 的
# entity.graphql，master）：
#   - updateDescription(input: DescriptionUpdateInput!): Boolean
#     DescriptionUpdateInput = {description!, resourceUrn!, subResourceType?, subResource?}
#     字段级用 subResourceType=DATASET_FIELD（该 enum 目前仅此一值）+ subResource=<fieldPath>；
#     数据集级省略二者。这是唯一的 canonical mutation（无 updateDatasetDescription）。
#   - addTerms(input: AddTermsInput!): Boolean
#     AddTermsInput = {termUrns![String!]!, resourceUrn!, subResourceType?, subResource?}
#   - setDomain(entityUrn: String!, domainUrn: String!): Boolean —— 位置参数，非 input 对象
# 三者在 master 均未 @deprecated、无近期形变。
#
# ⚠ **仍未在本目标实例的具体 DataHub 版本上核实**——版本极旧的服务端仍可能有差异。
# 因此服务层默认只做 preview，实际写入须显式 apply；首次 apply 必须在非生产实例上验证一次。

_MUTATION_UPDATE_DESCRIPTION = """
mutation updateDescription($input: DescriptionUpdateInput!) {
  updateDescription(input: $input)
}
"""

_MUTATION_ADD_TERMS = """
mutation addTerms($input: AddTermsInput!) {
  addTerms(input: $input)
}
"""

_MUTATION_SET_DOMAIN = """
mutation setDomain($entityUrn: String!, $domainUrn: String!) {
  setDomain(entityUrn: $entityUrn, domainUrn: $domainUrn)
}
"""

# 血缘上报（M11 兜底路径）。updateLineage 是 DataHub 开源 GraphQL 的 canonical
# mutation（datahub-graphql-core，master，未 @deprecated）：
#   updateLineage(input: UpdateLineageInput!): Boolean
#   UpdateLineageInput = {edgesToAdd: [LineageEdge!], edgesToRemove: [LineageEdge!]}
#   LineageEdge = {upstreamUrn: String!, downstreamUrn: String!}
# 加同一条边是幂等的（已存在则无副作用）——与 Airflow 插件重复上报不冲突。
# ⚠ 仅**表级**：字段级血缘（fineGrainedLineages）不在此 GraphQL 里，需 aspect
# 级 emitter，且受目标 DataHub 版本支持度制约（见 lineage_emitter 的说明）。
_MUTATION_UPDATE_LINEAGE = """
mutation updateLineage($input: UpdateLineageInput!) {
  updateLineage(input: $input)
}
"""


class DataHubWriteError(RuntimeError):
    """回写失败。保留原始 URN 与操作，便于定位与重放。"""

    def __init__(self, operation: str, urn: str, cause: Exception):
        self.operation = operation
        self.urn = urn
        self.cause = cause
        super().__init__(f"DataHub 回写失败（{operation} @ {urn}）：{cause}")


async def _mutate(connector: "DataHubConnector", operation: str, urn: str,
                  query: str, variables: dict) -> bool:
    try:
        data = await connector._graphql(query, variables)
    except Exception as exc:  # noqa: BLE001
        raise DataHubWriteError(operation, urn, exc) from exc
    return bool(data.get(operation, True))


async def update_dataset_description(
    connector: "DataHubConnector", urn: str, description: str
) -> bool:
    """写入数据集描述（业务语义由本体反补到元数据层）。"""
    return await _mutate(
        connector, "updateDescription", urn, _MUTATION_UPDATE_DESCRIPTION,
        {"input": {"resourceUrn": urn, "description": description}},
    )


async def update_field_description(
    connector: "DataHubConnector", dataset_urn: str, field_path: str, description: str
) -> bool:
    return await _mutate(
        connector, "updateDescription", dataset_urn, _MUTATION_UPDATE_DESCRIPTION,
        {
            "input": {
                "resourceUrn": dataset_urn,
                "description": description,
                "subResource": field_path,
                "subResourceType": "DATASET_FIELD",
            }
        },
    )


async def add_glossary_terms(
    connector: "DataHubConnector", urn: str, term_urns: list[str]
) -> bool:
    if not term_urns:
        return True
    return await _mutate(
        connector, "addTerms", urn, _MUTATION_ADD_TERMS,
        {"input": {"termUrns": term_urns, "resourceUrn": urn}},
    )


async def set_domain(connector: "DataHubConnector", urn: str, domain_urn: str) -> bool:
    return await _mutate(
        connector, "setDomain", urn, _MUTATION_SET_DOMAIN,
        {"entityUrn": urn, "domainUrn": domain_urn},
    )


async def add_lineage_edge(
    connector: "DataHubConnector", upstream_urn: str, downstream_urn: str
) -> bool:
    """上报一条表级血缘：``upstream_urn``（源表）→ ``downstream_urn``（目标表）。

    幂等：DataHub 对已存在的边不重复建。以 downstream 作定位 URN（血缘挂在下游侧）。
    """
    return await _mutate(
        connector, "updateLineage", downstream_urn, _MUTATION_UPDATE_LINEAGE,
        {
            "input": {
                "edgesToAdd": [
                    {"upstreamUrn": upstream_urn, "downstreamUrn": downstream_urn}
                ],
                "edgesToRemove": [],
            }
        },
    )
