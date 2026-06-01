"""
tests/test_ai_text_unit_scale.py
회귀 — AI(Gemini) 입력 텍스트의 금액 단위 환산.

버그(한국지엠 2025 연결감사보고서, 단위 백만원):
  경로B에서 Python은 BS/IS를 단위 보정해 정확히 추출(총자산 10,051,358 백만원 →
  10,051,358,000,000 원)하지만, AI 교차검증에 넘기는 텍스트는 "(단위: 백만원)" 라벨 +
  원시 숫자(10,051,358)였다. AI는 단위 변환을 하지 못하고 표시값을 그대로 돌려주어
  Python과 거대한 불일치가 났고, 판정(adjudicate)이 AI의 미보정 값(10,051,358)을
  채택해 단위 보정이 사라졌다.

수정: _extract_table_text_for_ai()가 테이블 단위를 적용해 숫자를 미리 원으로 환산하고
  라벨도 "(단위: 원)"으로 표기한다. AI는 숫자를 그대로 복사만 하면 되어 Python과 일치한다.
"""

from pathlib import Path

from financial.doc_extractor import (
    _parse_dart_xml,
    _extract_table_text_for_ai,
    _scale_amount_text,
)

_FIXTURE_XML = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "kgm_2025_path_b"
    / "raw_input.xml"
)


class TestScaleAmountText:
    def test_million_won_scaled_to_won(self):
        assert _scale_amount_text("10,051,358", 1_000_000) == "10,051,358,000,000"

    def test_thousand_won_scaled_to_won(self):
        assert _scale_amount_text("11,631,353", 1_000) == "11,631,353,000"

    def test_won_unit_left_unchanged(self):
        assert _scale_amount_text("12,345", 1) == "12,345"

    def test_parenthesized_negative_scaled(self):
        # 괄호 음수는 음수로 환산된다.
        assert _scale_amount_text("(20,283)", 1_000_000) == "-20,283,000,000"

    def test_non_numeric_cell_unchanged(self):
        assert _scale_amount_text("-", 1_000_000) == "-"
        assert _scale_amount_text("4", 1_000_000) == "4,000,000"  # 숫자면 환산


class TestKgmAiTextScaling:
    def test_bs_is_amounts_are_won_scaled_in_ai_text(self):
        soup = _parse_dart_xml(_FIXTURE_XML.read_bytes())
        text = _extract_table_text_for_ai(soup, fs_div="CFS")

        # 단위 라벨은 원으로 표기되고, 백만원 라벨이 남아있지 않아야 한다.
        assert "(단위: 원)" in text
        assert "백만원" not in text

        # 원문 백만원 표시값(10,051,358)이 원 단위(10,051,358,000,000)로 환산되어 있어야 한다.
        assert "10,051,358,000,000" in text   # 자본과부채총계 등
        assert "12,612,866,000,000" in text   # 매출액
        # 미보정 원시값이 그대로 남아있으면 안 된다.
        assert "총자산: 10,051,358\n" not in text
