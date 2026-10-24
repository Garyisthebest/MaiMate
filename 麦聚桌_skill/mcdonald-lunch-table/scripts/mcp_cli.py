#!/usr/bin/env python3
"""Read-only CLI and explicit two-phase mutation flow; no secret arguments."""
import argparse
import json
import sys
from pathlib import Path

from confirmation import ConfirmationLedger
from mcd_adapter import AdapterConfig, AdapterError, McdAdapter, ROOT, WRITE_TOOLS


def local_file(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise AdapterError("path_outside_project", "输入和输出文件必须在麦聚桌_skill目录内")
    return path


def read_json(value: str):
    return json.loads(local_file(value).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "call", "prepare", "confirm"])
    parser.add_argument("tool", nargs="?")
    parser.add_argument("--args", help="项目内 JSON 参数文件")
    parser.add_argument("--summary", help="项目内 JSON 预览文件（禁止含个人敏感偏好）")
    parser.add_argument("--scope", help="稳定桌/订单标识；同一桌不可换标识重发")
    parser.add_argument("--actor", help="桌主标识")
    parser.add_argument("--token-file", help="项目内短期确认令牌文件")
    parser.add_argument("--confirmed", action="store_true", help="仅在用户明确确认当前预览之后使用")
    opts = parser.parse_args()
    try:
        with McdAdapter(AdapterConfig.from_env()) as adapter:
            if opts.action == "discover":
                result = adapter.discover()
            else:
                if not opts.tool:
                    parser.error("tool is required")
                tool = adapter.resolve(opts.tool)
                args = read_json(opts.args) if opts.args else {}
                if opts.action == "call":
                    result = adapter.call(tool, args)
                else:
                    if not all([opts.summary, opts.scope, opts.actor, opts.token_file]):
                        parser.error("prepare/confirm requires --summary --scope --actor --token-file")
                    if tool not in WRITE_TOOLS:
                        raise AdapterError("unsupported_mutation", "工具不在写操作列表")
                    summary = read_json(opts.summary)
                    if not isinstance(summary, dict):
                        raise AdapterError("confirmation_invalid", "summary 必须是 JSON 对象")
                    token_file = local_file(opts.token_file)
                    ledger = ConfirmationLedger()
                    if opts.action == "prepare":
                        token = ledger.prepare(adapter, tool, args, summary, scope=opts.scope, actor=opts.actor)
                        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with token_file.open("w", encoding="utf-8") as handle:
                            token_file.chmod(0o600)
                            handle.write(token)
                        result = {"status": "awaiting_user_confirmation", "summary": summary,
                                  "token_file": str(token_file), "expires_in_seconds": 120}
                    else:
                        result = ledger.execute(adapter, tool, args, summary, token=token_file.read_text().strip(),
                                                scope=opts.scope, actor=opts.actor, confirmed=opts.confirmed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AdapterError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('{"ok":false,"code":"input_error","message":"项目内输入文件无法读取或JSON无效"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
