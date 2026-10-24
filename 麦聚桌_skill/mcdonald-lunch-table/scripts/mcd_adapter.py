"""Streamable HTTP MCP client for https://mcp.mcd.cn (protocol 2025-06-18).

The provider does not document server-side idempotency. Mutations are only
available through the local confirmation ledger, never generic call().
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from jsonschema import Draft202012Validator, SchemaError, ValidationError

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}
# Default deny: unknown/new tools are not assumed read-only even if annotated so.
READ_TOOLS = frozenset({
    "list-nutrition-foods", "delivery-query-addresses", "delivery-query-stores",
    "query-meal-assistance", "query-nearby-stores", "query-store-coupons",
    "query-meals", "query-meal-detail", "calculate-price", "query-order",
    "order-list", "campaign-calendar", "available-coupons", "query-my-coupons",
    "query-my-account", "mall-points-products", "mall-product-detail",
    "now-time-info", "query-lottery-info", "query-my-prizes", "mall-order-list",
    "mall-order-detail", "query-party-city", "query-party-store",
    "query-partystore-date", "query-partystore-session",
})
WRITE_TOOLS = frozenset({"create-order", "cancel-order", "delivery-create-address",
                         "auto-bind-coupons", "mall-create-order", "draw-lottery",
                         "party-order-create"})


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class AdapterConfig:
    url: str = "https://mcp.mcd.cn"
    key: str = ""
    timeout: float = 30.0
    tool_map_path: Path = ROOT / "mcdonald-lunch-table/config/tool-map.json"

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        # Only this project's .env; no implicit parent/global dotenv discovery.
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        key = (values.get("MCDONALD_MCP_KEY") or "").strip()
        if not key or key == "YOUR_MCP_TOKEN":
            raise AdapterError("config_missing", "请在麦聚桌_skill/.env 填写 MCDONALD_MCP_KEY")
        url = values.get("MCDONALD_MCP_URL") or "https://mcp.mcd.cn"
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AdapterError("config_invalid", "MCP URL 必须是 HTTPS，且不能含凭证、查询参数或片段")
        try:
            timeout = float(values.get("MCDONALD_MCP_TIMEOUT_SECONDS") or "30")
            if not 0 < timeout <= 300:
                raise ValueError
        except ValueError as exc:
            raise AdapterError("config_invalid", "timeout 必须在 0 到 300 秒之间") from exc
        return cls(url=url, key=key, timeout=timeout)


class McdAdapter:
    def __init__(self, config: AdapterConfig, client: httpx.Client | None = None):
        self.config = config
        # TLS verification remains enabled. No cross-host redirects with the token.
        self.client = client or httpx.Client(timeout=config.timeout, follow_redirects=False)
        self._request_id = 0
        self._session_id: str | None = None
        self._version: str | None = None
        self._initialized = False
        self.tools: dict[str, dict[str, Any]] | None = None
        try:
            self.tool_map = json.loads(config.tool_map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError("config_invalid", "无法读取项目 tool-map.json") from exc
        if not isinstance(self.tool_map, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in self.tool_map.items()
        ):
            raise AdapterError("config_invalid", "tool map 必须是内部接口到工具名称或 null 的映射")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "McdAdapter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.key}",
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._version:
            headers["MCP-Protocol-Version"] = self._version
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> Any:
        self._request_id += 1
        request_id = self._request_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = request_id
        try:
            # Stream to stop at the matching JSON-RPC reply, even if SSE stays open.
            with self.client.stream("POST", self.config.url, headers=self._headers(), json=body) as response:
                status = response.status_code
                if status >= 300:
                    codes = {401: "unauthorized", 403: "forbidden", 404: "session_or_endpoint_missing",
                             429: "rate_limited"}
                    raise AdapterError(codes.get(status, "http_error"), f"MCP HTTP {status}；未自动重试")
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id
                if notification:
                    return None
                ctype = response.headers.get("content-type", "").lower()
                if "text/event-stream" in ctype:
                    data: list[str] = []
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        elif not line and data:
                            payload = self._json("\n".join(data))
                            data = []
                            if isinstance(payload, dict) and payload.get("id") == request_id:
                                return self._result(payload, request_id)
                    if data:
                        payload = self._json("\n".join(data))
                        if isinstance(payload, dict) and payload.get("id") == request_id:
                            return self._result(payload, request_id)
                    raise AdapterError("protocol_error", "SSE 结束前没有收到匹配的 RPC 响应")
                if "application/json" not in ctype:
                    raise AdapterError("protocol_error", "响应既不是 JSON，也不是 SSE")
                payload = self._json(response.read())
                return self._result(payload, request_id)
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", "MCP 请求超时；写操作结果可能未知，禁止盲目重试") from exc
        except httpx.HTTPError as exc:
            # Never include request/response bodies, URLs or provider error text.
            raise AdapterError("network_error", "MCP 网络或证书验证失败；未自动重试") from exc

    @staticmethod
    def _json(raw: str | bytes) -> Any:
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("protocol_error", "MCP 返回了无效 JSON") from exc

    @staticmethod
    def _result(payload: Any, request_id: int) -> Any:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
            raise AdapterError("protocol_error", "JSON-RPC 版本或响应 ID 不匹配")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise AdapterError("mcp_error", f"MCP RPC 错误码 {code}")
        if "result" not in payload:
            raise AdapterError("protocol_error", "MCP 响应缺少 result")
        return payload["result"]

    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION,
            "capabilities": {}, "clientInfo": {"name": "maijuzhuo", "version": "0.2.0"}})
        if not isinstance(result, dict) or result.get("protocolVersion") not in SUPPORTED_VERSIONS:
            raise AdapterError("protocol_version", "服务器协商了不支持的 MCP 版本")
        self._version = result["protocolVersion"]
        self._rpc("notifications/initialized", notification=True)
        self._initialized = True

    def discover(self) -> dict[str, Any]:
        self.initialize()
        all_tools: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        params: dict[str, Any] = {}
        for _ in range(100):
            result = self._rpc("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise AdapterError("schema_error", "tools/list 必须返回 tools 数组")
            for tool in result["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                    raise AdapterError("schema_error", "工具必须包含名称和 inputSchema")
                try:
                    Draft202012Validator.check_schema(tool["inputSchema"])
                except SchemaError as exc:
                    raise AdapterError("schema_error", "工具 inputSchema 无效") from exc
                all_tools.append(tool)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise AdapterError("protocol_error", "tools/list 分页游标重复或无效")
            seen_cursors.add(cursor)
            params = {"cursor": cursor}
        else:
            raise AdapterError("protocol_error", "tools/list 超过 100 页")
        names = [tool["name"] for tool in all_tools]
        if len(set(names)) != len(names):
            raise AdapterError("schema_error", "tools/list 返回重复工具名称")
        self.tools = {tool["name"]: tool for tool in all_tools}
        return {"protocol_version": self._version, "tools": all_tools,
                "canonical": {k: {"tool": v, "available": v in self.tools} for k, v in self.tool_map.items()}}

    def resolve(self, name: str) -> str:
        if name in self.tool_map:
            tool = self.tool_map[name]
            if tool is None:
                raise AdapterError("capability_unavailable", f"官方未提供 {name}，不能模拟为其他接口")
            return tool
        return name

    def validate(self, tool: str, arguments: dict[str, Any]) -> None:
        if self.tools is None:
            self.discover()
        assert self.tools is not None
        if tool not in self.tools:
            raise AdapterError("capability_unavailable", f"服务未发现工具 {tool}")
        try:
            Draft202012Validator(self.tools[tool]["inputSchema"]).validate(arguments)
        except ValidationError as exc:
            # Error text can contain personal addresses/preferences; expose path only.
            path = ".".join(str(x) for x in exc.absolute_path) or "arguments"
            raise AdapterError("invalid_arguments", f"参数不满足实时 inputSchema：{path}") from exc

    def call(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self.resolve(name)
        if tool not in READ_TOOLS:
            raise AdapterError("confirmation_required", "写操作或未知工具禁止通用调用；请走本地确认流程")
        return self._execute(tool, params or {})

    def canonical_call(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call(name, params)

    def _execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validate(tool, arguments)
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if not isinstance(result, dict):
            raise AdapterError("schema_error", "tools/call 返回值必须是对象")
        if result.get("isError"):
            raise AdapterError("tool_error", "工具报告执行失败；详情未写入日志")
        data = self.business_payload(result)
        if isinstance(data, dict) and data.get("success") is False:
            # Runtime query-nearby-stores requires a city even though its input
            # schema marks city optional. Classify the observed error safely.
            if tool == 'query-nearby-stores' and data.get('code') == 600058:
                raise AdapterError('business_city_required', '官方门店查询需要有效城市')
            raise AdapterError("business_error", "麦当劳业务接口报告失败；未自动重试")
        return result

    @staticmethod
    def business_payload(result: dict[str, Any]) -> Any:
        """Preserve MCP envelopes; decode documented business JSON when available."""
        if "structuredContent" in result:
            return result["structuredContent"]
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    value = json.loads(block.get("text", ""))
                    if isinstance(value, dict):
                        return value
                except (ValueError, TypeError):
                    pass
        return None
