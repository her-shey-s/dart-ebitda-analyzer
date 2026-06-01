"""
tests/test_cf_column_and_number_parsing.py
회귀 — 연결현금흐름표 당기 컬럼 탐지 + 주석(노트번호) 컬럼 오인식 방지.

버그(동진홀딩스 2025 연결현금흐름표):
  표 구조가 [과목, 주석, 제 37 (당) 기, 제 36 (전) 기]이고 감가상각비 행은
  [감가상각비, '2,4,13,27,28,33'(주석 나열), '77,534,074,265'(당기), ...]였다.
    1) _detect_current_column이 헤더 '제 37 (당) 기'를 ((당) infix 때문에) 못 잡고,
       대신 데이터 행 '가. 당기순이익'의 '당기'를 헤더로 오인해 컬럼 0을 반환했다.
    2) 폴백이 첫 숫자 셀을 골랐는데, 그게 주석 컬럼 '2,4,13,27,28,33'였다.
       _parse_number가 콤마만 떼어 2,413,272,833(24억)으로 오인식 → 감가상각비가
       775억이 아니라 24억으로 추출됐다.

수정:
  - _parse_number: 콤마가 있으면 천단위 그룹 형식일 때만 숫자로 인정(노트 나열 거부).
  - _detect_current_column: '당기X'(당기순이익 등) 계정 라벨 제외, '제 N (당) 기'
    헤더를 잡도록 정규식 보강.

실제값(원): 감가상각비 77,534,074,265 + 투자부동산감가상각비 280,729,633
            = 77,814,803,898 / 무형자산상각비 2,379,890,868
"""

from pathlib import Path

from depreciation.extractor import (
    _parse_dart_xml,
    _parse_number,
    _detect_current_column,
    _xml_table_to_rows,
    _extract_from_cf,
)

_FIXTURE_XML = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "dongjin_hd_2025_path_b"
    / "raw_input.xml"
)


class TestParseNumber:
    def test_rejects_note_reference_list(self):
        # 주석 번호 나열은 금액이 아니다.
        assert _parse_number("2,4,13,27,28,33") is None
        assert _parse_number("2,14,27,28") is None

    def test_accepts_valid_thousands_grouped(self):
        assert _parse_number("77,534,074,265") == 77_534_074_265.0
        assert _parse_number("280,729,633") == 280_729_633.0

    def test_accepts_plain_and_parenthesized(self):
        assert _parse_number("335045000") == 335_045_000.0
        assert _parse_number("(1,234)") == -1234.0
        assert _parse_number("-") is None


class TestDetectCurrentColumn:
    def test_period_header_with_dang_infix(self):
        # "제 37 (당) 기" 헤더에서 당기 컬럼을 잡아야 한다(계정 라벨 '당기순이익' 무시).
        soup = _parse_dart_xml(_FIXTURE_XML.read_bytes())
        cf_rows = None
        for tbl in soup.find_all("table"):
            rows = _xml_table_to_rows(tbl)
            txt = " ".join(" ".join(r) for r in rows).replace(" ", "")
            if "영업활동으로인한현금흐름" in txt and "감가상각비" in txt:
                cf_rows = rows
                break
        assert cf_rows is not None
        col = _detect_current_column(cf_rows)
        # 당기 값(77,534,074,265)이 들어있는 컬럼이어야 한다(주석 컬럼 1이 아니라 2).
        depr_row = next(r for r in cf_rows if r and r[0].strip() == "감가상각비")
        assert col is not None
        assert _parse_number(depr_row[col]) == 77_534_074_265.0


class TestDongjinCfExtraction:
    def test_consolidated_cf_values(self):
        soup = _parse_dart_xml(_FIXTURE_XML.read_bytes())
        result = _extract_from_cf(soup, fs_div="CFS", strict_scope=False)
        # 감가상각비 = 유형 + 투자부동산
        assert result["감가상각비"] == 77_814_803_898.0
        assert result["무형자산상각비"] == 2_379_890_868.0
        assert result["combined"] is False
