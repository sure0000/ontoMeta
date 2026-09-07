"""DataHub schemaMetadata.foreignKeys REST 写回测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.connectors.datahub import (
    DataHubConnector,
    DataHubWriteError,
    add_foreign_key_constraints,
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, metadata: dict):
        self.metadata = metadata
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, dict]] = []

    async def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response({"aspect": {"com.linkedin.schema.SchemaMetadata": self.metadata}})

    async def post(self, url: str, *, json: dict, headers: dict):
        self.post_calls.append((url, json, headers))
        return _Response({"status": "accepted"})


def _connector(client: _Client) -> DataHubConnector:
    connector = DataHubConnector.__new__(DataHubConnector)
    connector.api_url = "http://datahub:8080"
    connector.token = "secret"
    connector._get_client = lambda: client  # type: ignore[method-assign]
    return connector


def test_foreign_key_write_merges_existing_schema_without_duplicates():
    metadata = {
        "schemaName": "orders",
        "platform": "urn:li:dataPlatform:mysql",
        "version": 1,
        "fields": [],
        "foreignKeys": [
            {
                "name": "existing",
                "sourceFields": ["shop_id"],
                "foreignFields": ["id"],
                "foreignDataset": "urn:li:dataset:(shop)",
            }
        ],
    }
    client = _Client(metadata)
    connector = _connector(client)

    result = asyncio.run(
        add_foreign_key_constraints(
            connector,
            "urn:li:dataset:(orders)",
            [
                {
                    "name": "new",
                    "source_field": "customer_id",
                    "target_field": "id",
                    "target_dataset": "urn:li:dataset:(customers)",
                },
                {
                    "name": "duplicate-name-is-ignored",
                    "source_field": "shop_id",
                    "target_field": "id",
                    "target_dataset": "urn:li:dataset:(shop)",
                },
            ],
        )
    )

    assert result is True
    assert len(client.get_calls) == 1
    assert len(client.post_calls) == 1
    payload = client.post_calls[0][1]
    proposal = payload["proposal"]
    assert proposal["aspectName"] == "schemaMetadata"
    written = json.loads(proposal["aspect"]["value"])
    assert len(written["foreignKeys"]) == 2
    assert written["fields"] == []
    assert client.post_calls[0][2]["Authorization"] == "Bearer secret"


def test_foreign_key_write_fails_closed_when_schema_aspect_is_missing():
    client = _Client({})

    async def run():
        connector = _connector(client)
        # Make the GET response look like a missing aspect.
        connector._get_client = lambda: _MissingAspectClient()  # type: ignore[method-assign]
        with pytest.raises(DataHubWriteError, match="schemaMetadata"):
            await add_foreign_key_constraints(
                connector,
                "urn:li:dataset:(orders)",
                [
                    {
                        "name": "new",
                        "source_field": "customer_id",
                        "target_field": "id",
                        "target_dataset": "urn:li:dataset:(customers)",
                    }
                ],
            )

    asyncio.run(run())


class _MissingAspectClient:
    async def get(self, url: str, **kwargs):
        return _Response({}, status_code=404)
