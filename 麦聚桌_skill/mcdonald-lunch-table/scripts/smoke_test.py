import json
from constraint_engine import MemberRequest, generate_plans


def main():
    menu = [
        {"sku": "burger", "name": "经典汉堡", "price": 25, "allergens": ["wheat"]},
        {"sku": "chicken", "name": "鸡肉汉堡", "price": 20, "allergens": ["wheat"]},
    ]
    plans = generate_plans([
        MemberRequest("A", ["burger"], budget_max=40),
        MemberRequest("B", ["burger"], budget_max=40, accepts_substitution=True),
    ], menu)
    assert len(plans) == 2 and plans[1]["total_amount"] < plans[0]["total_amount"]
    print(json.dumps({"mode": "offline_fixture", "plans": plans}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
