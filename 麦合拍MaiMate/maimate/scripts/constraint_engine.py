"""Offline candidate comparison only; authoritative totals require calculate-price."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import product
from typing import Any


@dataclass
class MemberRequest:
    member_id: str
    items: list[str]
    budget_max: float | None = None
    allergens: list[str] = field(default_factory=list)
    accepts_substitution: bool = False
    preferences: list[str] = field(default_factory=list)


def price(item: dict[str, Any]) -> Decimal:
    value = Decimal(str(item["price"]))
    if not value.is_finite() or value < 0:
        raise ValueError("invalid item price")
    return value


def allowed(item: dict[str, Any], member: MemberRequest) -> bool:
    if item.get("available") is False:
        return False
    if member.allergens:
        declarations = item.get("allergens")
        if not isinstance(declarations, list) or not all(isinstance(a, str) for a in declarations):
            return False
        # This only filters provided declarations, never proves safety.
        declared = {a.strip().casefold() for a in declarations}
        if declared & {a.strip().casefold() for a in member.allergens}:
            return False
    return True


def options(member: MemberRequest, menu: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe = [item for item in menu if allowed(item, member)]
    if not safe:
        raise ValueError(f"no confirmed-safe candidate for member {member.member_id}")
    requests = member.items or [""]
    pools = []
    for wanted in requests:
        exact = [item for item in safe if wanted.casefold() in {str(item.get("sku", "")).casefold(), str(item.get("name", "")).casefold()}]
        if wanted and not exact and not member.accepts_substitution:
            raise ValueError(f"requested item unavailable for member {member.member_id}; substitution not authorized")
        pools.append(safe if member.accepts_substitution or not wanted else exact)
    if len(requests) > 8 or len(menu) > 200:
        raise ValueError("offline planner supports at most 8 items/member and 200 menu entries")
    choices = []
    # Bounded search: never silently claim optimality for an incomplete search.
    combinations = 1
    for pool in pools:
        combinations *= len(pool)
    if combinations > 100000:
        raise ValueError("candidate space too large; narrow preferences first")
    for items in product(*pools):
        total = sum((price(i) for i in items), Decimal(0))
        if member.budget_max is not None and total > Decimal(str(member.budget_max)):
            continue
        changes = [{"member_id": member.member_id, "from": wanted, "to": item["name"], "reason": "用户允许替换的候选"}
                   for wanted, item in zip(requests, items)
                   if wanted and wanted.casefold() not in {str(item.get("sku", "")).casefold(), str(item.get("name", "")).casefold()}]
        preference_fit = sum(any(p.casefold() in str(i.get("name", "")).casefold() for i in items) for p in member.preferences)
        choices.append({"member_id": member.member_id, "items": list(items), "total": total, "changes": changes, "fit": preference_fit})
    if not choices:
        raise ValueError(f"no candidate within individual budget for member {member.member_id}")
    return choices


def generate_plans(members: list[MemberRequest], menu: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not members or not menu or len({m.member_id for m in members}) != len(members):
        raise ValueError("members/menu required; member IDs must be unique")
    choices = [options(m, menu) for m in members]
    stable = [min(pool, key=lambda c: (len(c["changes"]), -c["fit"], c["total"])) for pool in choices]
    cheapest = [min(pool, key=lambda c: (c["total"], len(c["changes"]), -c["fit"])) for pool in choices]
    plans = []
    seen = set()
    for plan_id, label, selection in [("stable", "稳妥原选", stable), ("savings", "省钱替换", cheapest)]:
        signature = tuple((c["member_id"], tuple(str(i.get("sku", i["name"])) for i in c["items"])) for c in selection)
        if signature in seen:
            continue
        seen.add(signature)
        changes = [change for c in selection for change in c["changes"]]
        total = sum((c["total"] for c in selection), Decimal(0))
        baseline = sum((c["total"] for c in stable), Decimal(0))
        plans.append({"plan_id": plan_id, "label": label,
            "items": [{"member_id": c["member_id"], "item": i} for c in selection for i in c["items"]],
            "total_amount": float(total), "hard_constraints_satisfied": True,
            "savings": float(baseline - total), "changes": changes,
            "tradeoffs": ["存在用户已允许的商品替换"] if changes else [],
            "estimated_ready_at": None, "price_source": "offline_fixture_requires_official_repricing",
            "allergen_note": "仅按已提供声明过滤；不能保证无交叉接触，须向门店核实"})
    return plans
