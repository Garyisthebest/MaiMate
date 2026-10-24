#!/usr/bin/env python3
"""Read-only discovery. Save tool schemas, never account/order data or Token."""
import json
import sys
from datetime import datetime, timezone
from mcd_adapter import AdapterConfig, AdapterError, McdAdapter, ROOT


def main() -> int:
    try:
        with McdAdapter(AdapterConfig.from_env()) as adapter:
            result = adapter.discover()
        out = ROOT / "runtime/capabilities.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"discovered_at": datetime.now(timezone.utc).isoformat(), **result}, ensure_ascii=False, indent=2))
        print(json.dumps({"ok": True, "protocol_version": result["protocol_version"], "tools": [t["name"] for t in result["tools"]], "saved_to": str(out)}, ensure_ascii=False))
        return 0
    except AdapterError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
