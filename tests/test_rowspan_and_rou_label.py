"""
tests/test_rowspan_and_rou_label.py
회귀 — (1) CF 행의 rowspan 그룹 헤더 보정, (2) 사용권자산상각비 라벨 substring 매칭.

버그 1 (STX엔진 2024 연결 CF '영업으로부터 창출된 현금' 조정):
  '감가상각비에 대한 조정' 행이 rowspan 그룹의 첫 하위 행이라, 그룹 헤더 셀이 row[0]에
  남아 실제 계정명이 row[1]로 밀렸다:
    ['당기순이익조정을 위한 가감', '감가상각비에 대한 조정', '9,683,232']
  row[0]만 보던 매칭이 이 행(96.8억)을 통째로 놓쳐 감가상각비가 투자부동산(17억)만
  잡혔다. 실제값(천원): 감가상각비 9,683,232 + 투자부동산 1,743,202 = 11,426,434 /
  무형자산상각비 495,337.

버그 2 (하나투어 2025 연결 CF 조정, 주석 경로):
  사용권자산상각비를 '사용권자산감가상각비 조정'으로 표기해, '조정' 접미 탓에
  exact-match 집합에 없어 검증 단계에서 null 처리됐다(174.9억 누락).

수정:
  - _account_label: 첫 숫자 셀 직전의 '한글 포함' 텍스트 셀 중 마지막을 계정 라벨로 본다.
    (주석 번호 나열 컬럼 '2,4,13,27,28,33'은 한글이 없어 제외된다.)
  - _is_rou_amortization_label: '사용권자산'+'상각'(누계 제외) substring 판정으로 통일.
"""

from depreciation.extractor import (
    _account_label,
    _is_rou_amortization_label,
    _extract_from_cf,
)


class TestAccountLabel:
    def test_rowspan_group_header_shifts_to_next_cell(self):
        # rowspan 그룹 첫 하위 행: 그룹 헤더(row[0]) 대신 실제 계정명을 집어야 한다.
        row = ["당기순이익조정을 위한 가감", "감가상각비에 대한 조정", "9,683,232"]
        assert _account_label(row) == "감가상각비에대한조정"

    def test_plain_label_row_uses_first_cell(self):
        assert _account_label(["무형자산상각비에 대한 조정", "495,337"]) == "무형자산상각비에대한조정"
        assert _account_label(["투자부동산감가상각비 조정", "1,743,202"]) == "투자부동산감가상각비조정"

    def test_note_reference_column_is_not_label(self):
        # 주석 번호 나열 컬럼은 숫자도 아니고 계정명도 아니다(동진홀딩스 2025).
        row = ["감가상각비", "2,4,13,27,28,33", "77,534,074,265", "73,000,000,000"]
        assert _account_label(row) == "감가상각비"


class TestRouAmortizationLabel:
    def test_rou_amortization_variants(self):
        assert _is_rou_amortization_label("사용권자산상각비") is True
        assert _is_rou_amortization_label("사용권자산감가상각비") is True
        # CF 조정항목의 '조정'·'에 대한 조정' 접미가 붙어도 매칭돼야 한다.
        assert _is_rou_amortization_label("사용권자산감가상각비조정") is True
        assert _is_rou_amortization_label("사용권자산상각비에대한조정") is True

    def test_non_rou_amortization(self):
        # 손상차손은 상각이 아니다. 누계는 자산 변동표의 상각누계액.
        assert _is_rou_amortization_label("사용권자산손상차손") is False
        assert _is_rou_amortization_label("사용권자산상각누계액") is False
        # 사용권자산이 아닌 라벨은 제외.
        assert _is_rou_amortization_label("무형자산상각비") is False
        assert _is_rou_amortization_label("감가상각비") is False


def _make_table(rows: list[list[str]], unit: int = 1000) -> str:
    cells = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table>{cells}</table>"


class TestRowspanCfExtraction:
    """STX엔진 2024 구조를 본뜬 합성 CF 조정 표로 감가상각비 합산을 검증한다."""

    def test_grouped_depreciation_row_is_summed(self):
        from bs4 import BeautifulSoup

        html = (
            "<html><body>"
            "<title>연결 현금흐름표</title>"
            "<table><tr><td>연결 현금흐름표</td></tr>"
            "<tr><td>영업활동 현금흐름</td><td>1,000</td></tr></table>"
            "<title>3. 연결재무제표 주석</title>"
            "<title>34. 현금흐름표 (연결)</title>"
            + _make_table([
                ["(단위: 천원)", ""],
                ["", "공시금액"],
                ["법인세비용차감전순이익(손실)", "22,259,933"],
                ["당기순이익조정을 위한 가감", "36,908,888"],
                # rowspan 그룹 첫 하위 행: 그룹 헤더 + 실제 계정명 + 값
                ["당기순이익조정을 위한 가감", "감가상각비에 대한 조정", "9,683,232"],
                ["무형자산상각비에 대한 조정", "495,337"],
                ["투자부동산감가상각비 조정", "1,743,202"],
                ["영업으로부터 창출된 현금흐름", "34,326,390"],
            ])
            + "</body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        result = _extract_from_cf(soup, fs_div="CFS", strict_scope=False)
        # 감가상각비 = 감가상각비에 대한 조정 + 투자부동산감가상각비 (단위 천원)
        assert result["감가상각비"] == (9_683_232 + 1_743_202) * 1000
        assert result["무형자산상각비"] == 495_337 * 1000
        assert result["combined"] is False
