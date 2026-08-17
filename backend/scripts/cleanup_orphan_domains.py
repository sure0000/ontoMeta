#!/usr/bin/env python3
"""
清理不在 DataHub 中的数据域

扫描数据库中的所有数据域，检查它们的 datahub_domain_id 是否在 DataHub 中存在，
删除不存在的数据域及其所有关联数据。

用法::

    cd backend && source .venv/bin/activate
    # 查看但不删除
    python -m scripts.cleanup_orphan_domains
    # 直接删除，不确认
    python -m scripts.cleanup_orphan_domains --yes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models.domain import DomainContext
from app.connectors.datahub import DataHubConnector


async def check_domain_exists(connector: DataHubConnector, domain_id: str) -> bool:
    """检查域是否在 DataHub 中存在"""
    try:
        # 尝试获取域列表，检查 domain_id 是否在其中
        domains = await connector.list_domains()
        return any(d.id == domain_id for d in domains)
    except Exception:
        return False


def delete_domain_cascade(db, domain_id: str) -> None:
    """级联删除数据域及其所有关联数据

    依赖关系链：
    domain -> ontology -> [business_logics, object_types, relation_types,
                          governance_artifacts, materialization_contracts, etc.]
    """
    # 使用原生 SQL 进行级联删除，避免 SQLAlchemy 的复杂依赖处理
    with engine.connect() as conn:
        # 1. 查找该域下的所有本体
        result = conn.execute(
            text("SELECT id FROM ontologies WHERE domain_context_id = :domain_id"),
            {"domain_id": domain_id}
        )
        ontology_ids = [row[0] for row in result]

        if ontology_ids:
            # 为 IN 子句构建参数
            ontology_params = {f"ont_{i}": ont_id for i, ont_id in enumerate(ontology_ids)}
            ont_placeholders = ", ".join(f":{k}" for k in ontology_params.keys())

            # 2. 删除所有依赖本体的数据
            tables_with_ontology_fk = [
                "semantic_index_entries",
                "data_app_widgets",
                "data_apps",
                "governance_task_pipelines",
                "draft_evidences",
                "draft_generation_tasks",
                "change_confirmations",
                "business_logic_property_bindings",  # 依赖 business_logics
                "business_logic_object_bindings",    # 依赖 business_logics
                "business_logics",
                "materialization_contracts",
                "governance_artifacts",
                "relation_types",
                "object_types",
            ]

            for table in tables_with_ontology_fk:
                try:
                    conn.execute(
                        text(f"DELETE FROM {table} WHERE ontology_id IN ({ont_placeholders})"),
                        ontology_params
                    )
                    conn.commit()
                except Exception as e:
                    print(f"    警告: 删除 {table} 时出错: {e}")
                    conn.rollback()

            # 3. 删除本体
            conn.execute(
                text(f"DELETE FROM ontologies WHERE id IN ({ont_placeholders})"),
                ontology_params
            )
            conn.commit()

        # 4. 删除数据域本身
        conn.execute(
            text("DELETE FROM domain_contexts WHERE id = :domain_id"),
            {"domain_id": domain_id}
        )
        conn.commit()


async def main_async(yes: bool = False):
    db = SessionLocal()
    connector = DataHubConnector()

    try:
        # 获取所有数据域
        domains = db.query(DomainContext).all()
        print(f"数据库中共有 {len(domains)} 个数据域")

        orphan_domains = []
        valid_domains = []

        for domain in domains:
            if not domain.datahub_domain_id:
                print(f"⚠️  域 '{domain.name}' (ID: {domain.id}) 没有 datahub_domain_id，跳过")
                continue

            # 检查 DataHub 中是否存在
            try:
                exists = await check_domain_exists(connector, domain.datahub_domain_id)
                if exists:
                    valid_domains.append(domain)
                    print(f"✓ 域 '{domain.name}' 在 DataHub 中存在")
                else:
                    orphan_domains.append(domain)
                    print(f"✗ 域 '{domain.name}' 在 DataHub 中不存在")
            except Exception as e:
                print(f"⚠️  检查域 '{domain.name}' 时出错: {e}")
                continue

        if not orphan_domains:
            print("\n✓ 没有需要清理的孤立数据域")
            return

        print(f"\n发现 {len(orphan_domains)} 个孤立数据域:")
        for domain in orphan_domains:
            print(f"  - {domain.name} (ID: {domain.id}, DataHub ID: {domain.datahub_domain_id})")

        # 确认删除
        if not yes:
            try:
                response = input(f"\n⚠️  警告: 这将删除数据域及其所有关联的本体、业务逻辑、治理制品等数据！\n是否继续删除这 {len(orphan_domains)} 个数据域? [y/N]: ")
                if response.lower() != 'y':
                    print("取消删除")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\n取消删除")
                return

        # 级联删除孤立数据域
        for domain in orphan_domains:
            print(f"\n删除域 '{domain.name}' (ID: {domain.id}) 及其所有关联数据...")
            try:
                delete_domain_cascade(db, domain.id)
                print(f"  ✓ 成功删除")
            except Exception as e:
                print(f"  ✗ 删除失败: {e}")

        print(f"\n✓ 清理完成")
        print(f"✓ 保留 {len(valid_domains)} 个有效数据域")

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        await connector.aclose()
        db.close()


def main():
    parser = argparse.ArgumentParser(description="清理不在 DataHub 中的数据域")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认，直接删除")
    args = parser.parse_args()

    asyncio.run(main_async(yes=args.yes))


if __name__ == "__main__":
    main()
