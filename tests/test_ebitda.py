"""
tests/test_ebitda.py
EBITDA 파생 계산 단위 테스트 (financial.ebitda) — 순수 함수, 네트워크/AI 불필요
"""

from financial.ebitda import compute_ebitda


def test_full_components():
    items = {"영업이익": 100, "감가상각비": 50, "사용권자산상각비": 10, "무형자산상각비": 5}
    assert compute_ebitda(items) == 165


def test_combined_disclosure_missing_addbacks_treated_as_zero():
    # 합산 공시: 무형/사용권이 None → 0으로 간주, 중복·과대계산 없음
    items = {"영업이익": 100, "감가상각비": 50, "사용권자산상각비": None, "무형자산상각비": None}
    assert compute_ebitda(items) == 150


def test_no_operating_income_is_none():
    items = {"영업이익": None, "감가상각비": 50, "무형자산상각비": 5}
    assert compute_ebitda(items) is None


def test_operating_loss_is_respected():
    items = {"영업이익": -30, "감가상각비": 50}
    assert compute_ebitda(items) == 20


def test_empty_items_is_none():
    assert compute_ebitda({}) is None
