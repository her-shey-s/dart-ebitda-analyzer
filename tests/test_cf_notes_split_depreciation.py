"""
tests/test_cf_notes_split_depreciation.py
회귀 — 증권·자산운용 등 금융사 현금흐름표 주석의 '자산별 분리 기재' 감가상각.

버그(한국투자증권 2025, 미래에셋자산운용 2025, 둘 다 경로A):
  현금흐름표 주석(35.)의 손익조정 항목에는 '감가상각비' 단일 행이 없고
  '유형자산감가상각비'·'투자부동산감가상각비'·'무형자산상각비'로 분리 기재된다.
  기존 코드는 두 군데서 이를 막아 D&A를 전부 공란/0으로 가져왔다:
    1) _detect_depreciation_signals: 정확한 '감가상각비' 행이 없어 시그널 게이트 탈락
       → 표가 후보에서 누락
    2) _verify_ai_selection: AI가 '유형자산감가상각비'를 지목해도 라벨이 정확
       '감가상각비'가 아니라 null 처리

수정: 일반 감가상각 라벨(_is_general_depreciation_label)을 시그널·검증 양쪽에서
  인정한다. Python이 같은 표의 유형자산+투자부동산 감가상각을 합산한다.

실제값(천원 단위, 원문 대조):
  한국투자증권: 유형 75,859,414 + 투자부동산 339,235 = 76,198,649 / 무형 20,654,755
  미래에셋자산운용: 유형 21,295,147 + 투자부동산 14,058,548 = 35,353,695 / 무형 5,100,731
"""

from pathlib import Path

import pytest

from depreciation.extractor import (
    _parse_dart_xml,
    _collect_depreciation_tables,
    _detect_depreciation_signals,
    _verify_ai_selection,
    _normalize_row_label,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# (fixture_id, 감가상각비_원, 무형자산상각비_원)
CASES = [
    ("kis_2025_path_a", 76_198_649_000, 20_654_755_000),
    ("miraeasset_am_2025_path_a", 35_353_695_000, 5_100_731_000),
]


def _load_soup(fixture_id: str):
    xml = (_FIXTURES / fixture_id / "raw_input.xml").read_bytes()
    return _parse_dart_xml(xml)


def _find_cf_note_table(tables: list[dict]) -> dict | None:
    """유형자산감가상각비 + 무형자산상각비가 함께 있는 CF 주석 후보 표를 찾는다."""
    for tbl in tables:
        labels = {_normalize_row_label(r[0]) for r in tbl["rows"] if r}
        if "유형자산감가상각비" in labels and "무형자산상각비" in labels:
            return tbl
    return None


def _row_id_for_label(rows, exact_label: str) -> str:
    for ri, row in enumerate(rows, 1):
        if row and _normalize_row_label(row[0]) == exact_label:
            return f"R{ri}"
    raise AssertionError(f"라벨 '{exact_label}' 행을 찾지 못함")


@pytest.mark.parametrize("fixture_id,expected_depr,expected_amort", CASES)
class TestSplitDepreciationSignals:
    def test_signal_detects_general_depreciation(
        self, fixture_id, expected_depr, expected_amort
    ):
        # CF 주석 표에 정확 '감가상각비' 행은 없지만 일반 감가상각 시그널은 잡혀야 한다.
        soup = _load_soup(fixture_id)
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        tbl = _find_cf_note_table(tables)
        assert tbl is not None, "CF 주석 분리기재 표가 후보로 수집되어야 한다"
        sig = _detect_depreciation_signals(tbl["rows"])
        assert sig["has_exact_depr"] is False
        assert sig["has_general_depr"] is True
        assert sig["has_separate_pair"] is True

    def test_verify_sums_tangible_and_investment_property(
        self, fixture_id, expected_depr, expected_amort
    ):
        soup = _load_soup(fixture_id)
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        tbl = _find_cf_note_table(tables)
        assert tbl is not None
        tid = f"T{tables.index(tbl) + 1}"
        rows = tbl["rows"]

        # Gemini가 가이드대로 '유형자산감가상각비'를 대표 행으로 지목하는 상황을 재현.
        ai_selection = {
            "depreciation": {
                "table_id": tid,
                "row_id": _row_id_for_label(rows, "유형자산감가상각비"),
                "row_label": "유형자산감가상각비",
            },
            "amortization": {
                "table_id": tid,
                "row_id": _row_id_for_label(rows, "무형자산상각비"),
                "row_label": "무형자산상각비",
            },
            "rou_amortization": None,
            "combined": False,
        }

        result = _verify_ai_selection(tables, ai_selection)
        # 감가상각비 = 유형자산 + 투자부동산 합산
        assert result["감가상각비"] == expected_depr
        assert result["무형자산상각비"] == expected_amort
        assert result["combined"] is False
