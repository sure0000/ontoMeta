import asyncio

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

MOCK_DOMAINS: list[DomainInput] = [
    DomainInput(
        id="urn:li:domain:customer",
        name="客户域",
        description="客户主数据、会员、标签等相关数据资产",
        owner="客户数据团队",
    ),
    DomainInput(
        id="urn:li:domain:order",
        name="订单域",
        description="订单、支付、履约相关数据资产",
        owner="交易数据团队",
    ),
    DomainInput(
        id="urn:li:domain:product",
        name="商品域",
        description="商品、品类、库存相关数据资产",
        owner="商品数据团队",
    ),
]

MOCK_DATASETS: dict[str, list[DatasetInput]] = {
    "urn:li:domain:customer": [
        DatasetInput(
            urn="urn:li:dataset:(urn:li:dataPlatform:hive,customer.dim_customer,PROD)",
            name="dim_customer",
            display_name="客户维表",
            description="客户主数据维表，包含客户基本信息与等级",
            platform="hive",
            container="customer",
            fields=[
                FieldInput(name="customer_id", display_name="客户ID", data_type="bigint", is_primary_key=True),
                FieldInput(name="customer_name", display_name="客户名称", data_type="string"),
                FieldInput(name="customer_level", display_name="客户等级", data_type="string"),
                FieldInput(name="register_date", display_name="注册日期", data_type="date"),
            ],
        ),
        DatasetInput(
            urn="urn:li:dataset:(urn:li:dataPlatform:hive,customer.ads_customer_tag,PROD)",
            name="ads_customer_tag",
            display_name="客户标签表",
            description="客户标签计算结果",
            platform="hive",
            container="customer",
            fields=[
                FieldInput(name="customer_id", display_name="客户ID", data_type="bigint", is_primary_key=True),
                FieldInput(name="high_value_tag", display_name="高价值标签", data_type="boolean"),
            ],
        ),
    ],
    "urn:li:domain:order": [
        DatasetInput(
            urn="urn:li:dataset:(urn:li:dataPlatform:hive,order.fact_order,PROD)",
            name="fact_order",
            display_name="订单事实表",
            description="订单交易事实表",
            platform="hive",
            container="order",
            fields=[
                FieldInput(name="order_id", display_name="订单ID", data_type="bigint", is_primary_key=True),
                FieldInput(
                    name="customer_id",
                    display_name="客户ID",
                    data_type="bigint",
                    is_foreign_key=True,
                    foreign_key_target="dim_customer.customer_id",
                ),
                FieldInput(name="order_amount", display_name="订单金额", data_type="decimal"),
                FieldInput(name="order_status", display_name="订单状态", data_type="string"),
                FieldInput(name="order_time", display_name="下单时间", data_type="timestamp"),
            ],
        ),
    ],
    "urn:li:domain:product": [
        DatasetInput(
            urn="urn:li:dataset:(urn:li:dataPlatform:hive,product.dim_product,PROD)",
            name="dim_product",
            display_name="商品维表",
            description="商品主数据",
            platform="hive",
            container="product",
            fields=[
                FieldInput(name="product_id", display_name="商品ID", data_type="bigint", is_primary_key=True),
                FieldInput(name="product_name", display_name="商品名称", data_type="string"),
                FieldInput(name="brand", display_name="品牌", data_type="string"),
                FieldInput(name="category", display_name="品类", data_type="string"),
            ],
        ),
    ],
}

MOCK_LINEAGES: dict[str, list[LineageInput]] = {
    "urn:li:domain:order": [
        LineageInput(
            source_urn="urn:li:dataset:(urn:li:dataPlatform:hive,customer.dim_customer,PROD)",
            target_urn="urn:li:dataset:(urn:li:dataPlatform:hive,order.fact_order,PROD)",
        ),
    ],
    "urn:li:domain:customer": [
        LineageInput(
            source_urn="urn:li:dataset:(urn:li:dataPlatform:hive,customer.dim_customer,PROD)",
            target_urn="urn:li:dataset:(urn:li:dataPlatform:hive,customer.ads_customer_tag,PROD)",
        ),
    ],
}

MOCK_LOGIC: dict[str, list[LogicEvidenceInput]] = {
    "urn:li:domain:customer": [
        LogicEvidenceInput(
            source_type="sql",
            source_ref="customer.ads_customer_tag",
            name="high_value_tag",
            expression="order_amount_90d >= 10000 AND order_count_90d >= 5",
            description="近90天高价值客户标签规则",
        ),
    ],
    "urn:li:domain:order": [
        LogicEvidenceInput(
            source_type="sql",
            source_ref="order.fact_order",
            name="gmv",
            expression="SUM(order_amount) WHERE order_status = 'paid'",
            description="已支付订单 GMV 指标",
        ),
    ],
}

_DATASET_ENTITY_FRAGMENT = """
fragment DatasetDetails on Dataset {
  urn
  name
  properties { description }
  platform { name }
  container { properties { name } }
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


def _parse_schema_fields(schema_metadata: dict | None) -> list[FieldInput]:
    if not schema_metadata:
        return []

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
        fields.append(
            FieldInput(
                name=name,
                display_name=field.get("description") or name,
                description=field.get("description"),
                data_type=field.get("nativeDataType") or field.get("type"),
                is_primary_key=name in primary_keys,
                is_foreign_key=is_foreign_key,
                foreign_key_target=fk_target,
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
        fields=_parse_schema_fields(raw.get("schemaMetadata")),
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


def _parse_query_logic_evidences(dataset: DatasetInput, queries: list[dict]) -> list[LogicEvidenceInput]:
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
            self.use_mock = env_settings.use_mock_datahub
        else:
            self.api_url = runtime_config.gms_url.rstrip("/")
            self.frontend_url = runtime_config.frontend_url.rstrip("/")
            self.token = runtime_config.token
            self.use_mock = runtime_config.use_mock

        # 懒初始化的复用 httpx 客户端，连接池跨多次 GraphQL 请求复用。
        # mock 模式下不会发起任何 HTTP 请求，无需创建。
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
        if self.use_mock:
            return MOCK_DOMAINS
        return await self._fetch_domains_from_api()

    async def fetch_domain_bundle(
        self,
        datahub_domain_id: str,
        *,
        include_logic_evidences: bool = False,
    ) -> DataHubDomainBundle:
        if self.use_mock:
            domain = next((d for d in MOCK_DOMAINS if d.id == datahub_domain_id), None)
            if not domain:
                raise ValueError(f"Domain not found: {datahub_domain_id}")
            return DataHubDomainBundle(
                domain=domain,
                datasets=MOCK_DATASETS.get(datahub_domain_id, []),
                lineages=MOCK_LINEAGES.get(datahub_domain_id, []),
                logic_evidences=(
                    MOCK_LOGIC.get(datahub_domain_id, [])
                    if include_logic_evidences
                    else []
                ),
            )
        return await self._fetch_domain_bundle_from_api(
            datahub_domain_id,
            include_logic_evidences=include_logic_evidences,
        )

    async def get_dataset_by_urn(self, dataset_urn: str) -> DatasetInput:
        if self.use_mock:
            for items in MOCK_DATASETS.values():
                for dataset in items:
                    if dataset.urn == dataset_urn:
                        return dataset
            raise ValueError(f"DataHub dataset not found: {dataset_urn}")

        entities = await self._fetch_dataset_entities([dataset_urn])
        if not entities:
            raise ValueError(f"DataHub dataset not found: {dataset_urn}")
        return _parse_dataset_entity(entities[0])

    def get_domain_url(self, datahub_domain_id: str) -> str:
        return f"{self.frontend_url}/domain/{datahub_domain_id}"

    def get_dataset_url(self, dataset_ref: str) -> str:
        from urllib.parse import quote

        urn = dataset_ref
        if not dataset_ref.startswith("urn:"):
            urn = f"urn:li:dataset:(urn:li:dataPlatform:hive,{dataset_ref},PROD)"
        return f"{self.frontend_url}/dataset/{quote(urn, safe='')}"

    async def search_datasets(self, query: str = "") -> list[DatasetInput]:
        if self.use_mock:
            keyword = query.strip().lower()
            all_datasets: list[DatasetInput] = []
            for items in MOCK_DATASETS.values():
                all_datasets.extend(items)
            if not keyword:
                return all_datasets
            return [
                ds
                for ds in all_datasets
                if keyword in (ds.name or "").lower()
                or keyword in (ds.display_name or "").lower()
                or keyword in (ds.description or "").lower()
                or keyword in ds.urn.lower()
            ]
        return await self._search_datasets_from_api(query)

    async def _search_datasets_from_api(self, query: str) -> list[DatasetInput]:
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
        data = await self._graphql(graphql_query, {"keyword": query})
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
        response = await client.post(
            f"{self.api_url}/api/graphql",
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise RuntimeError(str(payload["errors"]))
        return payload.get("data", {})
