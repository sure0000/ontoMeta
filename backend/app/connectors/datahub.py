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


class DataHubConnector:
    """对接 DataHub GraphQL / OpenAPI，输出统一内部输入模型。"""

    def __init__(self) -> None:
        self.base_url = settings.datahub_gms_url.rstrip("/")
        self.token = settings.datahub_token
        self.use_mock = settings.use_mock_datahub

    async def list_domains(self) -> list[DomainInput]:
        if self.use_mock:
            return MOCK_DOMAINS
        return await self._fetch_domains_from_api()

    async def fetch_domain_bundle(self, datahub_domain_id: str) -> DataHubDomainBundle:
        if self.use_mock:
            domain = next((d for d in MOCK_DOMAINS if d.id == datahub_domain_id), None)
            if not domain:
                raise ValueError(f"Domain not found: {datahub_domain_id}")
            return DataHubDomainBundle(
                domain=domain,
                datasets=MOCK_DATASETS.get(datahub_domain_id, []),
                lineages=MOCK_LINEAGES.get(datahub_domain_id, []),
                logic_evidences=MOCK_LOGIC.get(datahub_domain_id, []),
            )
        return await self._fetch_domain_bundle_from_api(datahub_domain_id)

    def get_domain_url(self, datahub_domain_id: str) -> str:
        return f"{self.base_url}/domain/{datahub_domain_id}"

    def get_dataset_url(self, dataset_ref: str) -> str:
        from urllib.parse import quote

        urn = dataset_ref
        if not dataset_ref.startswith("urn:"):
            urn = f"urn:li:dataset:(urn:li:dataPlatform:hive,{dataset_ref},PROD)"
        return f"{self.base_url}/dataset/{quote(urn, safe='')}"

    async def search_datasets(self, query: str = "") -> list[DatasetInput]:
        """按关键字搜索 DataHub datasets，未接入时返回 mock 全量。"""
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
            entities {
              urn
              ... on Dataset {
                name
                properties { description }
                platform { name }
              }
            }
          }
        }
        """
        data = await self._graphql(graphql_query, {"keyword": query})
        entities = data.get("search", {}).get("entities", [])
        return [
            DatasetInput(
                urn=item["urn"],
                name=item.get("name", item["urn"]),
                display_name=item.get("name"),
                description=item.get("properties", {}).get("description"),
                platform=item.get("platform", {}).get("name"),
            )
            for item in entities
        ]

    async def _fetch_domains_from_api(self) -> list[DomainInput]:
        query = """
        query listDomains {
          listDomains(input: { start: 0, count: 100 }) {
            domains {
              urn
              properties { name description }
            }
          }
        }
        """
        data = await self._graphql(query)
        domains = data.get("listDomains", {}).get("domains", [])
        return [
            DomainInput(
                id=item["urn"],
                name=item.get("properties", {}).get("name", item["urn"]),
                description=item.get("properties", {}).get("description"),
            )
            for item in domains
        ]

    async def _fetch_domain_bundle_from_api(self, datahub_domain_id: str) -> DataHubDomainBundle:
        # 简化实现：真实环境需扩展 GraphQL 查询
        domains = await self._fetch_domains_from_api()
        domain = next((d for d in domains if d.id == datahub_domain_id), None)
        if not domain:
            raise ValueError(f"Domain not found: {datahub_domain_id}")
        return DataHubDomainBundle(domain=domain)

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/api/graphql",
                json={"query": query, "variables": variables or {}},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            if "errors" in payload:
                raise RuntimeError(str(payload["errors"]))
            return payload.get("data", {})
