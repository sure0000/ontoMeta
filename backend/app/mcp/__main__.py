#!/usr/bin/env python3
"""
启动 ontoMeta MCP 服务器

用法：
    python -m app.mcp.server
"""
import asyncio
import sys
import os

# 添加 backend 目录到 Python 路径
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.mcp.server import main

if __name__ == "__main__":
    asyncio.run(main())
