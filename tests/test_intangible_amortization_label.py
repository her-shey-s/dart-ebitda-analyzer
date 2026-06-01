"""
tests/test_intangible_amortization_label.py
회귀 — '무형자산감가상각비' 표기를 무형자산상각비로 올바르게 분류.

버그(에실로코리아 2025, 경로B '26. 비용의 성격별 분류'):
  무형자산 상각비를 '무형자산상각비'가 아니라 '무형자산감가상각비'로 표기한 회사가 있다.
  이 라벨은 '무형자산'과 '상각' 사이에 '감가'가 끼어 '무형자산상각' substring으로 잡히지
  않았다. 그 결과:
    - _is_general_depreciation_label이 ('감가상각' 포함) True를 반환해 감가상각비 합산에
      무형자산상각비가 끼어 감가상각비가 부풀려졌다(3,867,439 + 335,045 = 4,202,484천원).
    - _EXACT_AMORT_LABELS에도 없어 무형자산상각비로 분리되지도 않았다.

수정: _is_intangible_amortization_label('무형자산'+'상각', 누계 제외)로 통일 판정한다.

실제값(천원, 원문 대조):
  감가상각비 3,867,439 / 무형자산상각비 335,045 / 사용권자산상각비 1,351,432
"""

from pathlib import Path

from depreciation.extractor import (
    _parse_dart_xml,
    _collect_depreciation_tables,
    _is_intangible_amortization_label,
    _is_general_depreciation_label,
    _verify_ai_selection,
    _normalize_row_label,
)

_FIXTURE_XML = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "essilor_2025_path_b"
    / "raw_input.xml"
)


class TestIntangibleAmortizationLabel:
    def test_intangible_amortization_variants(self):
        assert _is_intangible_amortization_label("무형자산상각비") is True
        assert _is_intangible_amortization_label("무형자산감가상각비") is True
        assert _is_intangible_amortization_label("무형자산상각") is True

    def test_non_intangible_amortization(self):
        # 손상차손/처분손실은 상각이 아니다.
        assert _is_intangible_amortization_label("무형자산손상차손") is False
        assert _is_intangible_amortization_label("무형자산처분손실") is False
        # 누계는 자산 변동표의 상각누계액
        assert _is_intangible_amortization_label("무형자산상각누계액") is False

    def test_intangible_amortization_excluded_from_general_depreciation(self):
        # '무형자산감가상각비'는 일반 감가상각(감가상각비 버킷)에서 제외돼야 한다.
        assert _is_general_depreciation_label("무형자산감가상각비") is False
        # 유형자산/투자부동산 감가상각은 여전히 일반 감가상각이다.
        assert _is_general_depreciation_label("유형자산감가상각비") is True
        assert _is_general_depreciation_label("투자부동산감가상각비") is True
        assert _is_general_depreciation_label("감가상각비") is True


def _find_expense_by_nature_table(tables: list[dict]) -> dict | None:
    """감가상각비 + 무형자산감가상각비가 같이 있는 성격별 분류 표를 찾는다."""
    for tbl in tables:
        labels = {_normalize_row_label(r[0]) for r in tbl["rows"] if r}
        if "감가상각비" in labels and "무형자산감가상각비" in labels:
            return tbl
    return None


class TestEssilorSplitBuckets:
    def test_amortization_not_folded_into_depreciation(self):
        soup = _parse_dart_xml(_FIXTURE_XML.read_bytes())
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=False)
        tbl = _find_expense_by_nature_table(tables)
        assert tbl is not None, "성격별 분류 표가 후보로 수집되어야 한다"
        tid = f"T{tables.index(tbl) + 1}"
        rows = tbl["rows"]

        def row_id(label: str) -> str:
            for ri, row in enumerate(rows, 1):
                if row and _normalize_row_label(row[0]) == label:
                    return f"R{ri}"
            raise AssertionError(f"라벨 '{label}' 없음")

        # Gemini가 가이드대로 지목하는 위치를 재현.
        ai_selection = {
            "depreciation": {
                "table_id": tid, "row_id": row_id("감가상각비"), "row_label": "감가상각비",
            },
            "rou_amortization": {
                "table_id": tid, "row_id": row_id("사용권자산상각비"), "row_label": "사용권자산상각비",
            },
            "amortization": {
                "table_id": tid, "row_id": row_id("무형자산감가상각비"), "row_label": "무형자산감가상각비",
            },
            "combined": False,
        }

        result = _verify_ai_selection(tables, ai_selection)
        # 감가상각비에 무형자산상각비가 합산되면 안 된다.
        assert result["감가상각비"] == 3_867_439_000
        assert result["무형자산상각비"] == 335_045_000
        assert result["사용권자산상각비"] == 1_351_432_000
        assert result["combined"] is False
