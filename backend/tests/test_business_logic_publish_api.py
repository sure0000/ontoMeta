"""口径的发布/删除端点。

由来：``publish_business_logic`` 用了 ``ConfirmationCreate`` 却没 import 它，任何一次
发布都是 500 ``NameError: name 'ConfirmationCreate' is not defined``。这条路径此前没有
任何用例覆盖，于是「指标口径发布不了」这件事一直没人看见——而指标编译要求口径已发布，
等于整条指标链路的入口是断的。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, Ontology, OntologyStatus


def _domain(tag: str) -> str:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:blpub-{tag}", name=f"blpub-{tag}"
        )
        db.add(domain)
        db.flush()
        # 创建口径要求该域已有**已发布**本体
        db.add(
            Ontology(
                domain_context_id=domain.id,
                status=OntologyStatus.PUBLISHED.value,
                version=1,
            )
        )
        db.commit()
        return domain.id


def _create(client, headers, domain_id: str, name: str) -> dict:
    r = client.post(
        "/api/business-logics",
        headers=headers,
        json={
            "domain_id": domain_id,
            "name": name,
            "display_name": "品牌数",
            "logic_type": "metric",
            "expression_summary": "COUNT(品牌.排序号)",
            "operator": "tester",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_publish_business_logic_returns_confirmation(client, admin_headers):
    logic = _create(client, admin_headers, _domain("ok"), "brand_count")
    r = client.post(
        f"/api/business-logics/{logic['id']}/publish", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_id"] == logic["id"]


def test_delete_business_logic(client, admin_headers):
    logic = _create(client, admin_headers, _domain("del"), "brand_count_del")
    r = client.delete(
        f"/api/business-logics/{logic['id']}", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
