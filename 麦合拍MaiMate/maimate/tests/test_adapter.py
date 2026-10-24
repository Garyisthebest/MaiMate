import json
from pathlib import Path
import httpx
import pytest
from mcd_adapter import AdapterConfig, AdapterError, McdAdapter, PROTOCOL_VERSION
from confirmation import ConfirmationLedger


class Server:
    def __init__(self, *, sse=False, fail_write=False):
        self.requests = []
        self.sse = sse
        self.fail_write = fail_write
        self.quote_amount = 2500

    def __call__(self, req):
        body = json.loads(req.content)
        self.requests.append(body)
        assert req.headers["Authorization"] == "Bearer test-token"
        assert req.headers["Accept"] == "application/json, text/event-stream"
        method = body["method"]
        if method == "initialize":
            assert body["params"]["protocolVersion"] == "2025-06-18"
            return httpx.Response(200, headers={"Mcp-Session-Id": "test-session"}, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}})
        assert req.headers["Mcp-Session-Id"] == "test-session"
        assert req.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
        if method == "notifications/initialized":
            assert "id" not in body
            return httpx.Response(202)
        if method == "tools/list":
            tools = [{"name": n, "inputSchema": {"type": "object", "required": ["storeCode"], "properties": {"storeCode": {"type": "string"}}, "additionalProperties": False}} for n in ["query-meals", "create-order", "calculate-price"]]
            result = {"tools": tools}
        else:
            assert method == "tools/call"
            assert set(body["params"]) == {"name", "arguments"}
            if body["params"]["name"] == "create-order" and self.fail_write:
                raise httpx.ReadTimeout("lost response", request=req)
            data = {"price": self.quote_amount, "productList": []} if body["params"]["name"] == "calculate-price" else {"orderId": "fixture-order"}
            result = {"content": [{"type": "text", "text": json.dumps({"success": True, "data": data})}]}
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            # Includes a notification before the correlated reply, split data lines.
            encoded = json.dumps(payload, indent=2)
            stream = ': ping\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            stream += '\n'.join('data: ' + line for line in encoded.splitlines()) + '\n\n'
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=stream)
        return httpx.Response(200, json=payload)


def make_adapter(tmp_path, **options):
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({"menu.get": "query-meals", "order.submit": "create-order", "cart.create": None}))
    server = Server(**options)
    adapter = McdAdapter(AdapterConfig(key="test-token", tool_map_path=mapping), httpx.Client(transport=httpx.MockTransport(server)))
    return adapter, server


def test_runtime_city_business_error_has_safe_specific_classification(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    adapter.tools = {'query-nearby-stores': {'inputSchema': {'type': 'object'}}}
    adapter._rpc = lambda *args, **kwargs: {'structuredContent': {
        'success': False, 'code': 600058, 'message': 'private-provider-data', 'traceId': 'private-trace'}}
    with pytest.raises(AdapterError) as error:
        adapter.call('query-nearby-stores', {'searchType': 2, 'beType': 1, 'keyword': '示例广场店'})
    assert error.value.code == 'business_city_required'
    assert '城市' in str(error.value)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize("sse", [True, False])
def test_protocol_init_session_discovery_and_tools_call(tmp_path, sse):
    adapter, server = make_adapter(tmp_path, sse=sse)
    result = adapter.call("menu.get", {"storeCode": "fixture"})
    assert result["content"][0]["type"] == "text"
    assert [r["method"] for r in server.requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


def test_arguments_checked_before_remote_call(tmp_path):
    adapter, server = make_adapter(tmp_path)
    with pytest.raises(AdapterError, match="inputSchema"):
        adapter.call("menu.get", {})
    assert not any(x["method"] == "tools/call" for x in server.requests)


@pytest.mark.parametrize("tool", ["order.submit", "create-order", "auto-bind-coupons", "new-tool"])
def test_generic_calls_cannot_bypass_confirmation(tmp_path, tool):
    adapter, server = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as err:
        adapter.call(tool, {})
    assert err.value.code == "confirmation_required"
    assert server.requests == []


def test_missing_capability_is_explicit(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as err:
        adapter.call("cart.create")
    assert err.value.code == "capability_unavailable"


def test_confirmation_binds_preview_actor_and_arguments(tmp_path):
    adapter, server = make_adapter(tmp_path)
    ledger = ConfirmationLedger(tmp_path / "ledger.sqlite3")
    args = {"storeCode": "fixture"}
    summary = {"total_cents": 2500}
    token = ledger.prepare(adapter, "create-order", args, summary, scope="table-a", actor="owner")
    for overrides in [{"args": {"storeCode": "other"}}, {"summary": {"total_cents": 2600}}, {"actor": "other"}, {"confirmed": False}]:
        values = dict(adapter=adapter, tool="create-order", args=args, summary=summary, token=token, scope="table-a", actor="owner", confirmed=True)
        values.update(overrides)
        with pytest.raises(AdapterError):
            ledger.execute(**values)
    assert not any(r.get("params", {}).get("name") == "create-order" for r in server.requests)
    ledger.execute(adapter, "create-order", args, summary, token=token, scope="table-a", actor="owner", confirmed=True)
    with pytest.raises(AdapterError):
        ledger.execute(adapter, "create-order", args, summary, token=token, scope="table-a", actor="owner", confirmed=True)
    with pytest.raises(AdapterError):
        ledger.prepare(adapter, "create-order", args, summary, scope="table-a", actor="owner")


def test_timeout_remains_blocked_after_restart(tmp_path):
    adapter, server = make_adapter(tmp_path, fail_write=True)
    path = tmp_path / "ledger.sqlite3"
    ledger = ConfirmationLedger(path)
    args, summary = {"storeCode": "fixture"}, {"total_cents": 2500}
    token = ledger.prepare(adapter, "create-order", args, summary, scope="table-a", actor="owner")
    with pytest.raises(AdapterError) as err:
        ledger.execute(adapter, "create-order", args, summary, token=token, scope="table-a", actor="owner", confirmed=True)
    assert err.value.code == "timeout"
    restarted = ConfirmationLedger(path)
    with pytest.raises(AdapterError):
        restarted.prepare(adapter, "create-order", args, summary, scope="table-a", actor="owner")
    calls = [r for r in server.requests if r.get("params", {}).get("name") == "create-order"]
    assert len(calls) == 1


def test_rate_limit_and_auth_no_retry_or_secret_in_errors(tmp_path):
    for status, expected in [(401, "unauthorized"), (429, "rate_limited")]:
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(status, text="test-token must never appear in diagnostics")
        adapter, _ = make_adapter(tmp_path)
        adapter.client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterError) as err:
            adapter.discover()
        assert err.value.code == expected
        assert "test-token" not in str(err.value)
        assert len(calls) == 1


def test_changed_price_invalidates_confirmation_before_submit(tmp_path):
    adapter, server = make_adapter(tmp_path)
    ledger = ConfirmationLedger(tmp_path / "ledger.sqlite3")
    args, summary = {"storeCode": "fixture"}, {"total_cents": 2500}
    token = ledger.prepare(adapter, "create-order", args, summary, scope="table-a", actor="owner")
    server.quote_amount = 2600
    with pytest.raises(AdapterError) as err:
        ledger.execute(adapter, "create-order", args, summary, token=token, scope="table-a", actor="owner", confirmed=True)
    assert err.value.code == "price_changed"
    assert not any(r.get("params", {}).get("name") == "create-order" for r in server.requests)
    # A new quote and explicit confirmation can be prepared after a pre-submit failure.
    ledger.prepare(adapter, "create-order", args, {"total_cents": 2600}, scope="table-a", actor="owner")


def test_expired_token_is_rejected(tmp_path):
    adapter, server = make_adapter(tmp_path)
    ledger = ConfirmationLedger(tmp_path / "ledger.sqlite3")
    args, summary = {"storeCode": "fixture"}, {"total_cents": 2500}
    token = ledger.prepare(adapter, "create-order", args, summary, scope="create:table-a", actor="owner")
    with ledger.connect() as db:
        db.execute("UPDATE confirmations SET expires=0")
    with pytest.raises(AdapterError):
        ledger.execute(adapter, "create-order", args, summary, token=token, scope="create:table-a", actor="owner", confirmed=True)
    assert not any(r.get("params", {}).get("name") == "create-order" for r in server.requests)


def test_new_preview_supersedes_old_token(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    ledger = ConfirmationLedger(tmp_path / "ledger.sqlite3")
    args, summary = {"storeCode": "fixture"}, {"total_cents": 2500}
    old_token = ledger.prepare(adapter, "create-order", args, summary, scope="create:table-a", actor="owner")
    ledger.prepare(adapter, "create-order", args, summary, scope="create:table-a", actor="owner")
    with pytest.raises(AdapterError):
        ledger.execute(adapter, "create-order", args, summary, token=old_token, scope="create:table-a", actor="owner", confirmed=True)


def test_newer_server_protocol_is_rejected(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25"}})
    adapter.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as err:
        adapter.discover()
    assert err.value.code == "protocol_version"


def test_discovery_paginates(tmp_path):
    adapter, server = make_adapter(tmp_path)
    def handler(req):
        body = json.loads(req.content)
        if body["method"] != "tools/list":
            return server(req)
        if body["params"].get("cursor") == "page-2":
            result = {"tools": [{"name": "second", "inputSchema": {"type": "object"}}]}
        else:
            result = {"tools": [{"name": "first", "inputSchema": {"type": "object"}}], "nextCursor": "page-2"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    adapter.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert [t["name"] for t in adapter.discover()["tools"]] == ["first", "second"]
