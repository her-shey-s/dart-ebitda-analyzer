"""
tests/test_extraction_rules.py
추출 규칙 단위 테스트 — 결정적, 네트워크/AI 불필요

1) 감가상각비 합산 규칙 (우진기전 2023 케이스)
   현금흐름표 조정 주석에 '감가상각비'와 '투자부동산 감가상각비'가 분리 기재될 때,
   사용권/무형은 제외하고 나머지 감가상각 항목만 '감가상각비'로 합산해야 한다.
   중복 계산(사용권/무형이 감가상각비에 더해짐)이 절대 없어야 한다.

2) 결손금 부호 보정 (파워맥스 2023 케이스)
   '결손금' 계열 라벨로 매칭된 이익잉여금은 무조건 음수여야 한다.
"""

import pytest

from config import ITEM_MAP
from depreciation.extractor import (
    _is_general_depreciation_label,
    _sum_general_depreciation_in_table,
)
from financial.doc_extractor import find_item_detail_in_table


# ── 1) 감가상각비 합산 규칙 ────────────────────────────────────────────────────
def test_general_depreciation_label_classification():
    # 합산 대상
    assert _is_general_depreciation_label("감가상각비") is True
    assert _is_general_depreciation_label("투자부동산감가상각비") is True
    assert _is_general_depreciation_label("유형자산감가상각비") is True
    # 제외 대상
    assert _is_general_depreciation_label("사용권자산감가상각비") is False  # 사용권 버킷
    assert _is_general_depreciation_label("무형자산상각비") is False        # 무형 버킷
    assert _is_general_depreciation_label("대손상각비") is False            # 감가상각 아님
    assert _is_general_depreciation_label("감가상각누계액") is False         # 누계액(자산변동표)


def test_sum_general_depreciation_excludes_rou_and_amort():
    # 우진기전 2023 현금흐름 조정표 구조 (단위 천원)
    tables = [{
        "unit": 1000,
        "rows": [
            ["구분", "당기", "전기"],
            ["당기순이익", "25,295,210", "26,265,148"],
            ["감가상각비", "434,335", "399,058"],
            ["투자부동산 감가상각비", "15,309", "22,835"],
            ["무형자산상각비", "194,245", "215,340"],
            ["사용권자산 감가상각비", "850,497", "783,753"],
            ["대손상각비", "1,149,540", "2,567,961"],
        ],
    }]
    total = _sum_general_depreciation_in_table(tables, "T1")
    # 감가상각비 434,335 + 투자부동산 15,309 = 449,644 천원 (사용권/무형/대손 제외)
    assert total == (434335 + 15309) * 1000


# ── 2) 결손금 부호 보정 ────────────────────────────────────────────────────────
def _retained_earnings_value(rows: list[list[str]]):
    item = ITEM_MAP["이익잉여금"]
    detail = find_item_detail_in_table(
        "이익잉여금",
        rows,
        keywords=item["keywords"],
        negate_keywords=item.get("negate_keywords"),
        statement="BS",
    )
    return detail["value"] if detail else None


def test_deficit_label_forced_negative():
    rows = [["과목", "당기"], ["결손금", "11,141,285,652"]]
    assert _retained_earnings_value(rows) == -11_141_285_652


def test_undisposed_deficit_label_forced_negative():
    rows = [["과목", "당기"], ["미처리결손금", "16,623,744,095"]]
    assert _retained_earnings_value(rows) == -16_623_744_095


def test_positive_retained_earnings_not_negated():
    rows = [["과목", "당기"], ["이익잉여금", "5,000,000"]]
    assert _retained_earnings_value(rows) == 5_000_000


# (라벨, 당기값) → 기대 결과. 결손금이면 무조건 음수, 흑자 이익잉여금이면 양수.
# 연도별로 이익잉여금/결손금이 뒤섞이는 보고서를 모두 커버한다.
@pytest.mark.parametrize(
    "label, cell, expected",
    [
        # ① 이익잉여금 라벨 + 원문 음수(괄호) → 결손금(음수)
        ("이익잉여금", "(11,141,285,652)", -11_141_285_652),
        # ② 이익잉여금(결손금) 라벨 + 원문 음수 → 결손금(음수)
        ("이익잉여금(결손금)", "(11,141,285,652)", -11_141_285_652),
        # ③ 결손금 라벨 + 양수 표기 → 결손금(음수)로 보정
        ("결손금", "11,141,285,652", -11_141_285_652),
        ("미처리결손금", "16,623,744,095", -16_623_744_095),
        # 정상 흑자: 부호 유지(양수)
        ("이익잉여금", "5,000,000", 5_000_000),
        ("이익잉여금(결손금)", "5,000,000", 5_000_000),
        # 결손금 라벨이 이미 음수면 그대로 음수 유지
        ("결손금", "(11,141,285,652)", -11_141_285_652),
    ],
)
def test_retained_earnings_sign_matrix(label, cell, expected):
    rows = [["과목", "당기"], [label, cell]]
    assert _retained_earnings_value(rows) == expected
