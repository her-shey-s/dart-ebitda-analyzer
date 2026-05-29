"""
tests/test_stxengine_2022_notes_unit.py
회귀 — STX엔진 2022 사업보고서 주석 '34. 현금흐름표' 추출 (연결).

두 가지 버그를 잠근다:
  1. 단위 오인식: 주석에 재수록된 '34. 현금흐름표' 표는 인라인으로 "(단위: 천원)"을
     달고 있으나, 단위 감지가 상위 '2. 연결재무제표' 섹션의 "(단위: 백만원)" 캡션을
     먼저 집어 1,000배(천원→백만원) 오차가 발생했다. 인라인 표기를 부모 형제보다
     먼저 보도록 고쳐 천원(=1,000)으로 감지되어야 한다.
  2. 투자부동산상각비 누락: '투자부동산상각비'는 라벨에 '감가'가 빠진 단축 표기라
     일반 감가상각 합산에서 제외됐다. EBITDA 가산을 위해 감가상각비에 합산되어야 한다.

실제값(연결, 단위 천원):
  감가상각비 11,631,353 + 투자부동산상각비 1,743,202 = 13,374,555 (천원)
  무형자산상각비 931,629 (천원)
"""

from pathlib import Path

from depreciation.extractor import (
    _parse_dart_xml,
    _collect_depreciation_tables,
    _sum_general_depreciation_in_table,
    _verify_ai_selection,
    _xml_table_to_rows,
    _normalize_row_label,
)
from utils.units import detect_unit_multiplier

# 실제 보고서 원문과 대조해 검증한 정답값(연결, 원 단위).
# 주석 '34. 현금흐름표' 표 (단위: 천원):
#   감가상각비 11,631,353 + 투자부동산상각비 1,743,202 = 13,374,555 (천원)
#   무형자산상각비 931,629 (천원), 사용권자산상각비 미기재
GOLDEN_ITEMS = {
    "감가상각비": 13_374_555_000,
    "무형자산상각비": 931_629_000,
    "사용권자산상각비": None,
}

_FIXTURE_XML = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "stxengine_2022_path_a"
    / "raw_input.xml"
)


def _load_soup():
    return _parse_dart_xml(_FIXTURE_XML.read_bytes())


def _find_note34_table(soup):
    """주석 '34. 현금흐름표'의 감가/무형/투자부동산 상각 표 태그를 찾는다."""
    for tbl in soup.find_all("table"):
        rows = _xml_table_to_rows(tbl)
        txt = " ".join(" ".join(r) for r in rows)
        if "투자부동산상각비" in txt and "감가상각비" in txt:
            return tbl
    return None


class TestNote34UnitDetection:
    def test_inline_thousand_won_wins_over_parent_million_won(self):
        # 인라인 "(단위: 천원)"이 상위 재무제표 섹션 "(단위: 백만원)"보다 우선.
        soup = _load_soup()
        tbl = _find_note34_table(soup)
        assert tbl is not None
        assert detect_unit_multiplier(tbl) == 1_000

    def test_collected_candidate_unit_is_thousand_won(self):
        soup = _load_soup()
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        note34 = [t for t in tables if "현금흐름표" in (t.get("title") or "")]
        assert note34, "주석 34.현금흐름표 후보 테이블이 수집되어야 한다"
        assert note34[0]["unit"] == 1_000


class TestNote34InvestmentPropertyAggregation:
    def test_general_depreciation_sum_includes_investment_property(self):
        soup = _load_soup()
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        tid = next(
            f"T{i}"
            for i, t in enumerate(tables, 1)
            if "현금흐름표" in (t.get("title") or "")
        )
        total = _sum_general_depreciation_in_table(tables, tid)
        # 11,631,353 천원 + 1,743,202 천원 = 13,374,555 천원 = 13,374,555,000 원
        assert total == 13_374_555_000


class TestNote34GoldenValues:
    """주석 경로의 최종 산출값을 실제 정답값(원 단위)으로 고정한다.

    실제 추출은 AI(Gemini) 표/행 선택을 거치지만 오프라인 테스트에서는 비활성화되므로,
    Gemini가 지목할 위치(감가상각비 행 / 무형자산상각비 행)를 그대로 시뮬레이션해
    Python 검증·합산 단계(_verify_ai_selection)의 산출값만 결정적으로 검증한다.
    """

    def _row_id_for_label(self, rows, exact_label):
        for ri, row in enumerate(rows, 1):
            if row and _normalize_row_label(row[0]) == exact_label:
                return f"R{ri}"
        raise AssertionError(f"라벨 '{exact_label}' 행을 찾지 못함")

    def test_notes_path_matches_golden_values(self):
        soup = _load_soup()
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        ti, tbl = next(
            (i, t)
            for i, t in enumerate(tables, 1)
            if "현금흐름표" in (t.get("title") or "")
        )
        tid = f"T{ti}"
        rows = tbl["rows"]

        # Gemini가 지목할 위치를 그대로 재현한 선택 결과.
        ai_selection = {
            "depreciation": {
                "table_id": tid,
                "row_id": self._row_id_for_label(rows, "감가상각비"),
                "row_label": "감가상각비",
            },
            "amortization": {
                "table_id": tid,
                "row_id": self._row_id_for_label(rows, "무형자산상각비"),
                "row_label": "무형자산상각비",
            },
            "rou_amortization": None,
            "combined": False,
        }

        result = _verify_ai_selection(tables, ai_selection)

        assert result["감가상각비"] == GOLDEN_ITEMS["감가상각비"]
        assert result["무형자산상각비"] == GOLDEN_ITEMS["무형자산상각비"]
        assert result["사용권자산상각비"] == GOLDEN_ITEMS["사용권자산상각비"]
        assert result["combined"] is False
