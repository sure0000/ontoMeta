#!/usr/bin/env python3
"""
测试 MCP 工具

直接调用工具验证功能，无需启动完整的 MCP 服务器。
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.mcp.tools import query, TOOL_REGISTRY, AuthContext


async def test_query_ontology():
    """测试 query_ontology 工具"""
    print("=" * 60)
    print("测试: query_ontology")
    print("=" * 60)

    tool = TOOL_REGISTRY.get("query_ontology")
    if not tool:
        print("❌ 工具未找到")
        return

    print(f"✓ 工具已注册: {tool.name}")
    print(f"  描述: {tool.description}")
    print()

    # 创建测试认证上下文
    auth = AuthContext(user_id=None, client_type="test")

    # 测试 1: 查询所有已发布的本体
    print("测试 1: 查询所有已发布的本体")
    print("-" * 60)
    result = await tool.execute({"include_unpublished": False}, auth)

    if result.success:
        print(f"✓ 成功")
        print(f"  找到 {result.metadata['count']} 个本体")
        if result.data["ontologies"]:
            for ont in result.data["ontologies"][:3]:  # 只显示前3个
                print(f"  - {ont['id']}: {ont['domain_name']} (v{ont['version']})")
            if len(result.data["ontologies"]) > 3:
                print(f"  ... 还有 {len(result.data['ontologies']) - 3} 个")
    else:
        print(f"❌ 失败: {result.error}")

    print()

    # 测试 2: 查询包含未发布的本体
    print("测试 2: 查询包含未发布的本体")
    print("-" * 60)
    result = await tool.execute({"include_unpublished": True}, auth)

    if result.success:
        print(f"✓ 成功")
        print(f"  找到 {result.metadata['count']} 个本体")
        published_count = sum(1 for o in result.data["ontologies"] if o["published"])
        unpublished_count = result.metadata["count"] - published_count
        print(f"  - 已发布: {published_count}")
        print(f"  - 未发布: {unpublished_count}")
    else:
        print(f"❌ 失败: {result.error}")

    print()

    # 测试 3: 查询特定本体（使用第一个本体的 ID）
    if result.success and result.data["ontologies"]:
        first_ont_id = result.data["ontologies"][0]["id"]
        print(f"测试 3: 查询特定本体 ({first_ont_id})")
        print("-" * 60)

        result = await tool.execute(
            {"ontology_id": first_ont_id, "include_unpublished": True}, auth
        )

        if result.success:
            print(f"✓ 成功")
            ont = result.data["ontologies"][0]
            print(f"  ID: {ont['id']}")
            print(f"  域名: {ont['domain_name']}")
            print(f"  版本: {ont['version']}")
            print(f"  已发布: {ont['published']}")
            if ont["description"]:
                print(f"  描述: {ont['description'][:100]}")
        else:
            print(f"❌ 失败: {result.error}")

    print()
    print("=" * 60)


async def main():
    """运行所有测试"""
    print()
    print("ontoMeta MCP 工具测试")
    print()

    await test_query_ontology()

    print()
    print("测试完成!")
    print()


if __name__ == "__main__":
    asyncio.run(main())
