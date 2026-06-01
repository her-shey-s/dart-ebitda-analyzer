"""
tests/test_expense_by_nature_combined.py
회귀 — '비용의 성격별 분류' 표의 합산/일반 감가상각 행 추출.

두 버그를 잠근다(둘 다 경로A, 값이 0/공란으로 나왔음):
  1) 롯데물산 2025 '32. 비용의 성격별 분류': '감가상각비 및 무형자산상각비' 99,541(백만원)
     - report_finder가 본문 XML이 없는 [첨부정정] 보고서를 골라 문서 파싱이 통째로 실패했다.
       → [첨부정정]은 첨부서류만 정정하므로 비-[첨부정정](원본)을 우선하도록 수정.
     - 합산 행이므로 combined 처리되어 감가상각비에 99,541백만원이 들어가야 한다.
  2) 이랜드월드 2025 '31. 비용의 성격별 분류': '감가상각비와 기타상각비' 382,884(백만원)
     - 행 라벨 '상품매출원가' 때문에 표가 functional_breakdown으로 오분류되어 제외됐다.
       → 기능별 토큰은 컬럼 헤더(0번 라벨 컬럼 제외)에서만 보고, '비용의 성격별 분류'
         제목은 기능별 판정보다 우선하도록 수정.
"""

from pathlib import Path

import pytest

from depreciation.extractor import (
    _parse_dart_xml,
    _collect_depreciation_tables,
    _classify_depreciation_candidate,
    _verify_ai_selection,
    _normalize_row_label,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _find_expense_table(tables, combined_label):
    for tbl in tables:
        labels = {_normalize_row_label(r[0]) for r in tbl["rows"] if r}
        if combined_label in labels:
            return tbl
    return None


def _row_id(rows, label):
    for ri, row in enumerate(rows, 1):
        if row and _normalize_row_label(row[0]) == label:
            return f"R{ri}"
    raise AssertionError(f"라벨 '{label}' 없음")


class TestExpenseByNatureNotMisclassified:
    def test_elandworld_table_is_expense_by_nature_not_functional(self):
        # '상품매출원가' 행이 있어도 '비용의 성격별 분류' 표는 functional_breakdown이
        # 아니라 expense_by_nature 여야 한다(제외되면 안 됨).
        soup = _parse_dart_xml((_FIXTURES / "elandworld_2025_path_a" / "raw_input.xml").read_bytes())
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        tbl = _find_expense_table(tables, "감가상각비와기타상각비")
        assert tbl is not None, "성격별 분류 표가 후보에서 제외되면 안 된다"
        ct, _flags = _classify_depreciation_candidate(tbl["rows"], tbl.get("title"))
        assert ct == "expense_by_nature"


class TestCombinedExpenseByNatureValues:
    @pytest.mark.parametrize(
        "fixture_id,combined_label,expected_depr,expected_display",
        [
            ("lottems_2025_path_a", "감가상각비및무형자산상각비", 99_541_000_000, "감가상각비 및 무형자산상각비"),
            ("elandworld_2025_path_a", "감가상각비와기타상각비", 382_884_000_000, "감가상각비와 기타상각비"),
        ],
    )
    def test_combined_row_goes_to_depreciation(
        self, fixture_id, combined_label, expected_depr, expected_display
    ):
        soup = _parse_dart_xml((_FIXTURES / fixture_id / "raw_input.xml").read_bytes())
        tables = _collect_depreciation_tables(soup, fs_div="CFS", strict_scope=True)
        tbl = _find_expense_table(tables, combined_label)
        assert tbl is not None
        tid = f"T{tables.index(tbl) + 1}"
        rows = tbl["rows"]

        # Gemini가 합산 행을 depreciation으로 지목하는 상황 재현.
        ai_selection = {
            "depreciation": {
                "table_id": tid,
                "row_id": _row_id(rows, combined_label),
                "row_label": combined_label,
            },
            "rou_amortization": None,
            "amortization": None,
            "combined": True,
        }
        result = _verify_ai_selection(tables, ai_selection)
        assert result["감가상각비"] == expected_depr
        # 비고용 합산 라벨은 원문 표기(공백 유지)를 그대로 보존해야 한다.
        assert result["combined_label"] == expected_display


class TestReportFinderPrefersNonAttachmentCorrection:
    def test_attachment_correction_skipped_in_favor_of_original(self, monkeypatch):
        # [첨부정정]은 본문 XML이 없으므로 원본을 우선해야 한다.
        import dart_api.report_finder as rf

        same_day = "20260331"
        fake_items = [
            {"rcept_no": "20260331004817", "report_nm": "[첨부정정]사업보고서 (2025.12)", "rcept_dt": same_day},
            {"rcept_no": "20260331004115", "report_nm": "사업보고서 (2025.12)", "rcept_dt": same_day},
        ]

        def fake_fetch(corp_code, bgn_de, end_de, pblntf_detail_ty):
            # 사업보고서(annual) spec에서만 결과를 주고 나머지는 빈 목록.
            return fake_items if pblntf_detail_ty == "A001" else []

        monkeypatch.setattr(rf, "_fetch_disclosures", fake_fetch)
        report = rf.find_report("00120483", 2025)
        assert report is not None
        assert report["rcept_no"] == "20260331004115"
        assert "[첨부정정]" not in report["report_nm"]
