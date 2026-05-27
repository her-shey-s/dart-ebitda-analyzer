"""
tests/test_fs_div_scope.py
경로B/본문 fallback의 연결·별도(fs_div) 추론 회귀 테스트 — 결정적, 네트워크/AI 불필요

배경(실제 회귀 케이스):
  피케이밸브앤엔지니어링 2022 사업보고서는 별도 전용 회사인데도 본문에
  "3.연결재무제표주석" 같은 '빈 연결 섹션 헤더' 타이틀이 있어, 과거에는
  fs_div가 CFS(연결)로 오판되었다. 이 오판이 감가상각 추출 스코프까지
  연쇄적으로 망가뜨려 감가상각비를 누락시켰다.

  → _infer_path_b_fs_div는 '연결 재무제표가 데이터와 함께 실재할 때만' CFS로
    판정해야 한다.
"""

from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

from financial.doc_extractor import (
    _has_populated_consolidated_statements,
    _infer_path_b_fs_div,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _soup(xml: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(xml, "lxml")


# ── 합성 케이스: 로직 자체를 정밀하게 고정 ───────────────────────────────────
_EMPTY_CONSOLIDATED_HEADER = """<DOCUMENT>
<TITLE>2. 연결재무제표</TITLE>
<TITLE>3. 연결재무제표주석</TITLE>
<TITLE>4. 재무제표</TITLE>
<TABLE>
  <TR><TD>과목</TD><TD>당기</TD></TR>
  <TR><TD>자산총계</TD><TD>188,922,000,000</TD></TR>
</TABLE>
<TITLE>5. 재무제표주석</TITLE>
</DOCUMENT>"""

_POPULATED_CONSOLIDATED = """<DOCUMENT>
<TITLE>2. 연결재무제표</TITLE>
<TITLE>연결 재무상태표</TITLE>
<TABLE>
  <TR><TD>과목</TD><TD>당기</TD></TR>
  <TR><TD>자산총계</TD><TD>1,000,000,000,000</TD></TR>
</TABLE>
<TITLE>4. 재무제표</TITLE>
<TABLE>
  <TR><TD>과목</TD><TD>당기</TD></TR>
  <TR><TD>자산총계</TD><TD>900,000,000,000</TD></TR>
</TABLE>
</DOCUMENT>"""


def test_empty_consolidated_section_header_is_separate():
    # 빈 연결 섹션 헤더만 있고 실제 값은 별도 섹션에 있음 → OFS
    soup = _soup(_EMPTY_CONSOLIDATED_HEADER)
    assert _has_populated_consolidated_statements(soup) is False
    assert _infer_path_b_fs_div(soup, report_type="annual") == "OFS"


def test_populated_consolidated_is_consolidated():
    # 연결 섹션에 실제 금액이 채워진 표가 있음 → CFS
    soup = _soup(_POPULATED_CONSOLIDATED)
    assert _has_populated_consolidated_statements(soup) is True
    assert _infer_path_b_fs_div(soup, report_type="annual") == "CFS"


def test_report_type_override_wins():
    # report_finder가 이미 연결/별도를 식별했으면 그 값이 우선
    soup = _soup(_EMPTY_CONSOLIDATED_HEADER)
    assert _infer_path_b_fs_div(soup, report_type="audit_consol") == "CFS"
    assert _infer_path_b_fs_div(_soup(_POPULATED_CONSOLIDATED), report_type="audit_separate") == "OFS"


# ── 실제 보고서 회귀 락 ───────────────────────────────────────────────────────
def test_real_pkvalve_2022_is_separate():
    xml_path = FIXTURES / "pkvalve_2022_path_a_fallback" / "raw_input.xml"
    if not xml_path.is_file():
        import pytest
        pytest.skip("실제 fixture 원본(raw_input.xml)이 없습니다.")
    soup = _soup(xml_path.read_bytes())
    # 별도 전용 회사: 연결 재무제표 본문이 데이터와 함께 실재하지 않는다.
    assert _has_populated_consolidated_statements(soup) is False
    assert _infer_path_b_fs_div(soup, report_type="annual") == "OFS"


def test_real_pkvalve_2022_scope_filter_reads_separate_bs():
    """
    스코프 필터 회귀 락(명세 #4): 빈 연결재무제표 섹션 + 채워진 별도 재무제표가
    혼재된 사업보고서 본문에서, AI 없이도 별도(OFS) 스코프의 BS를 읽어 총자산이
    0이 아닌 실제값(≈188.9조)으로 나와야 한다. 스코프 필터 이전에는 첫 매칭
    표(빈 연결)를 읽어 총자산=0이 되었다.
    """
    xml_path = FIXTURES / "pkvalve_2022_path_a_fallback" / "raw_input.xml"
    if not xml_path.is_file():
        import pytest
        pytest.skip("실제 fixture 원본(raw_input.xml)이 없습니다.")

    xml = xml_path.read_bytes()
    with ExitStack() as stack:
        # AI 비활성 → 순수 파이썬 추출(결정적). 다운로드 경계만 fixture로 대체.
        stack.enter_context(mock.patch("config.get_gemini_api_key", lambda: ""))
        stack.enter_context(
            mock.patch(
                "financial.doc_extractor._download_dart_document", lambda rcept_no: xml
            )
        )
        from financial.doc_extractor import get_financial_data_path_b

        result = get_financial_data_path_b("20230331001846", report_type="annual")

    assert result["error"] is None
    assert result["fs_div"] == "OFS"
    assert result["ai_comparison"] is None  # AI 비활성 확인 → 값은 파이썬 추출
    assert result["items"]["총자산"] == 188_922_000_000.0
