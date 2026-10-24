import pytest
from constraint_engine import MemberRequest, generate_plans

MENU = [{"sku": "burger", "name": "Burger", "price": 20, "allergens": []},
        {"sku": "chicken", "name": "Chicken", "price": 10, "allergens": []}]


def test_allergen_unknown_is_not_treated_safe():
    with pytest.raises(ValueError, match="confirmed-safe"):
        generate_plans([MemberRequest("A", ["burger"], allergens=["peanut"])], [{"sku": "burger", "name": "Burger", "price": 10}])


def test_does_not_mask_individual_budget_with_group_budget():
    with pytest.raises(ValueError, match="individual budget"):
        generate_plans([MemberRequest("A", ["burger"], budget_max=10), MemberRequest("B", ["chicken"], budget_max=100)], MENU)


def test_substitution_requires_authorization():
    with pytest.raises(ValueError, match="substitution not authorized"):
        generate_plans([MemberRequest("A", ["missing"])], MENU)


def test_real_alternative_is_cheaper_and_explained():
    plans = generate_plans([MemberRequest("A", ["burger"], accepts_substitution=True)], MENU)
    assert len(plans) == 2
    assert plans[1]["total_amount"] == 10
    assert plans[1]["changes"][0]["from"] == "burger"
    assert plans[1]["estimated_ready_at"] is None


def test_identical_plans_are_not_relabelled_as_alternatives():
    plans = generate_plans([MemberRequest("A", ["burger"])], MENU)
    assert len(plans) == 1


def test_out_of_stock_never_recommended():
    menu = [{**MENU[0], "available": False}, MENU[1]]
    with pytest.raises(ValueError):
        generate_plans([MemberRequest("A", ["burger"])], menu)
