"""
tests/test_value_state.py
명세 #5 회귀 테스트 — 단위/당기 컬럼 신뢰도, 0과 None 의미 구분

핵심 회귀:
  과거에는 당기 컬럼 셀이 숫자가 아니면 무조건 0으로 잠그고 끝냈다 — "-"(0)/
  빈칸(미기재)/N/A(해당없음)/주석 표기 등이 한 통계에 섞였다. 이제 의미별로
  분류하고, 대시만 0을 유지하며 나머지는 다음 표/행으로 넘어간다.
"""

from financial.doc_extractor import (
    _classify_non_numeric_cell,
    _detect_current_column,
    find_item_detail_in_table,
)


# ── 비숫자 셀 분류 ────────────────────────────────────────────────────────────
class TestClassifyNonNumericCell:
    def test_dash_variants_are_zero(self):
        for raw in ("-", "—", "–", "−", "ー"):
            assert _classify_non_numeric_cell(raw) == "zero", raw
        # 대시만으로 구성된 다중 문자 (예: "---")도 zero
        assert _classify_non_numeric_cell("---") == "zero"

    def test_empty_or_whitespace_is_missing(self):
        for raw in (None, "", "   ", "\t"):
            assert _classify_non_numeric_cell(raw) == "missing", raw

    def test_na_tokens_are_not_applicable(self):
        for raw in ("N/A", "n/a", "NA", "해당없음", "해당사항없음"):
            assert _classify_non_numeric_cell(raw) == "not_applicable", raw

    def test_other_strings_are_parse_failed(self):
        for raw in ("주석20", "%", "abc", "당기"):
            assert _classify_non_numeric_cell(raw) == "parse_failed", raw

    def test_numeric_strings_are_not_classified_here(self):
        # 헬퍼는 숫자 여부를 판단하지 않는다(호출측이 _to_float를 먼저 시도).
        # 숫자 문자열도 일반 문자열로 들어오면 parse_failed로 분류된다.
        assert _classify_non_numeric_cell("1,234") == "parse_failed"


# ── 당기 컬럼 탐지 신뢰도 ────────────────────────────────────────────────────
class TestDetectCurrentColumnConfidence:
    def test_dangi_keyword(self):
        rows = [["과목", "당기", "전기"], ["자산총계", "100", "90"]]
        col, conf = _detect_current_column(rows)
        assert (col, conf) == (1, "detected_dangi")

    def test_period_max(self):
        rows = [["과목", "제 23 기", "제 22 기"], ["자산총계", "100", "90"]]
        col, conf = _detect_current_column(rows)
        assert (col, conf) == (1, "detected_period_max")

    def test_year_max(self):
        rows = [["과목", "2024", "2023"], ["자산총계", "100", "90"]]
        col, conf = _detect_current_column(rows)
        assert (col, conf) == (1, "detected_year_max")

    def test_not_detected_when_header_lacks_signals(self):
        rows = [["과목", "값A", "값B"], ["자산총계", "100", "90"]]
        col, conf = _detect_current_column(rows)
        assert col is None
        assert conf == "not_detected"


# ── 비숫자 현재 셀 → 0 잠금 방지 / current_col_confidence 기록 ────────────────
_KEYWORDS = ["자산총계"]


def _detail(rows):
    return find_item_detail_in_table(
        "총자산", rows, _KEYWORDS,
        unit_multiplier=1, unit_confidence="detected_from_tag",
        statement="BS", table_id="BS1",
    )


class TestNonNumericCurrentCell:
    def test_dash_yields_zero_with_low_confidence(self):
        # "-"는 항목 존재·금액 0의 관례. 값 0 유지하되 verified는 아님.
        rows = [["과목", "당기"], ["자산총계", "-"]]
        d = _detail(rows)
        assert d is not None
        assert d["value"] == 0.0
        assert d["confidence"] == "low_confidence"
        assert d["value_state"] == "zero_from_dash"
        assert "current_cell_dash_classified_zero" in d["flags"]
        assert d["source"]["current_col_confidence"] == "detected_dangi"

    def test_empty_current_cell_does_not_lock_zero(self):
        # 빈 현재 셀은 다음 매칭 행 또는 다음 표로 넘어가야 한다.
        # 동일 표 내 다른 매칭 행이 없으면 None 반환.
        rows = [["과목", "당기"], ["자산총계", ""]]
        assert _detail(rows) is None

    def test_garbage_current_cell_falls_through_to_next_matching_row(self):
        # 첫 매칭 행의 현재 셀이 비숫자(주석 표기 등)면 같은 표 내 다음 매칭 행을 시도.
        rows = [
            ["과목", "당기"],
            ["자산총계", "주석20"],   # parse_failed → continue
            ["자산총계", "188,922"],
        ]
        d = _detail(rows)
        assert d is not None
        assert d["value"] == 188922.0
        assert d["confidence"] == "verified"

    def test_na_does_not_lock_zero(self):
        rows = [["과목", "당기"], ["자산총계", "N/A"]]
        assert _detail(rows) is None


# ── current_col_confidence가 결과에 기록되는지 ───────────────────────────────
class TestCurrentColConfidenceRecorded:
    def test_recorded_on_normal_extraction(self):
        rows = [["과목", "당기"], ["자산총계", "100"]]
        d = _detail(rows)
        assert d["source"]["current_col_confidence"] == "detected_dangi"
        assert d["confidence"] == "verified"

    def test_fallback_first_numeric_degrades_to_low_confidence(self):
        # 헤더에 당기/제N기/연도가 없으면 첫 숫자 컬럼 fallback이고 verified가 아니다.
        rows = [["과목", "구분A", "구분B"], ["자산총계", "100", "200"]]
        d = _detail(rows)
        assert d is not None
        assert d["source"]["current_col_confidence"] == "fallback_first_numeric"
        assert d["confidence"] == "low_confidence"
        assert "first_numeric_column_used" in d["flags"]


# ── 단위 미확인 시 verified가 아님 (#5 완료조건) ─────────────────────────────
class TestUnitAssumedDowngradesConfidence:
    def test_unit_assumed_won_blocks_verified(self):
        rows = [["과목", "당기"], ["자산총계", "100"]]
        d = find_item_detail_in_table(
            "총자산", rows, _KEYWORDS,
            unit_multiplier=1, unit_confidence="assumed_won",  # ← 단위 미확인
            statement="BS", table_id="BS1",
        )
        assert d is not None
        assert d["confidence"] != "verified"
        assert "unit_assumed_won" in d["flags"]
