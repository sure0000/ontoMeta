#!/usr/bin/env python3
"""ontoMeta MCP stdio 桥接：供 Cursor 等 MCP Client 通过 stdio 接入。

将 stdin 上的 MCP JSON-RPC 转发到后端 HTTP 端点 /api/mcp。
环境变量：
  ONTOMETA_MCP_URL  默认 http://127.0.0.1:8000/api/mcp
  ONTOMETA_API_KEY  必填，外部应用 API Key
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

MCP_URL = os.environ.get("ONTOMETA_MCP_URL", "http://127.0.0.1:8000/api/mcp").rstrip("/")
API_KEY = os.environ.get("ONTOMETA_API_KEY", "").strip()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _http_rpc(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not API_KEY:
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {
                "code": -32000,
                "message": "缺少 ONTOMETA_API_KEY 环境变量",
            },
        }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 204:
                return None
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
            detail = parsed.get("detail") or err_body
        except Exception:
            detail = err_body or str(exc)
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32000, "message": f"HTTP {exc.code}: {detail}"},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32000, "message": f"连接 ontoMeta 失败: {exc}"},
        }


def _write(msg: dict[str, Any] | None) -> None:
    if msg is None:
        return
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    _log(f"ontoMeta MCP stdio bridge → {MCP_URL}")
    if not API_KEY:
        _log("警告: 未设置 ONTOMETA_API_KEY")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            )
            continue

        method = payload.get("method")
        # Cursor 可能发 notifications/initialized，无 id，后端返回 204
        result = _http_rpc(payload)
        if result is None and payload.get("id") is not None:
            # 保底：若后端对某些方法无响应，避免 Cursor 挂起
            if method == "notifications/initialized":
                continue
            result = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32603, "message": "Empty response from server"},
            }
        _write(result)


if __name__ == "__main__":
    main()
