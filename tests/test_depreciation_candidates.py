"""
tests/test_depreciation_candidates.py
명세 #7 회귀 테스트 — 감가상각 후보 분류기와 위험 플래그 전파

핵심:
  과거에는 자산 변동표/판관비/기능별 배분 표 같은 부분값 테이블이 1차 키워드/제목
  필터를 운 좋게 통과해 AI 후보에 남으면, AI가 그것을 선택해도 Python 검증은 숫자만
  맞추고 의미 오류는 놓쳤다. 이제 구조 기반 분류기로 후보 단계에서 잘라내고,
  살아남은 후보에도 risk_flags를 붙여 결과 신뢰도에 반영한다.
"""

from bs4 import BeautifulSoup

from depreciation.extractor import (
    _build_depreciation_item_details,
    _classify_depreciation_candidate,
    _collect_depreciation_tables,
)


def _soup(xml: str) -> BeautifulSoup:
    return BeautifulSoup(xml, "lxml")


# ── 분류기 단위 테스트 ────────────────────────────────────────────────────────
class TestClassifier:
    def test_asset_movement_by_headers(self):
        # 취득원가 + 감가상각누계액 + 장부금액 → asset_movement
        rows = [
            ["구분", "취득원가", "감가상각누계액", "장부금액"],
            ["감가상각비", "10", "5", "5"],
        ]
        ctype, flags = _classify_depreciation_candidate(rows, title="유형자산")
        assert ctype == "asset_movement"
        assert "asset_movement_headers" in flags

    def test_asset_movement_by_flow_columns(self):
        # 기초·취득·처분·대체·기말 등 흐름 컬럼 ≥3 → asset_movement
        rows = [
            ["구분", "기초", "취득", "처분", "대체", "기말"],
            ["감가상각비", "10", "5", "3", "1", "13"],
        ]
        ctype, flags = _classify_depreciation_candidate(rows, title="유형자산 증감")
        assert ctype == "asset_movement"
        assert "asset_movement_flow_columns" in flags

    def test_functional_breakdown_by_columns(self):
        # 판매비와관리비/매출원가 컬럼 → functional_breakdown
        rows = [
            ["구분", "판매비와관리비", "매출원가", "합계"],
            ["감가상각비", "10", "20", "30"],
        ]
        ctype, flags = _classify_depreciation_candidate(rows, title="판매비와 관리비")
        assert ctype == "functional_breakdown"
        assert "functional_breakdown_columns" in flags

    def test_expense_by_nature_strong_positive(self):
        # 섹션 제목에 "비용의 성격별 분류" → 강한 긍정
        rows = [
            ["구분", "당기", "전기"],
            ["감가상각비", "100", "90"],
            ["무형자산상각비", "10", "9"],
        ]
        ctype, flags = _classify_depreciation_candidate(
            rows, title="비용의 성격별 분류"
        )
        assert ctype == "expense_by_nature"
        assert flags == []

    def test_general_depreciation_default(self):
        # 위 분류에 속하지 않는 평범한 표 → general_depreciation
        rows = [
            ["구분", "당기", "전기"],
            ["감가상각비", "100", "90"],
        ]
        ctype, flags = _classify_depreciation_candidate(rows, title="기타 비용")
        assert ctype == "general_depreciation"
        assert flags == []


# ── _collect_depreciation_tables 통합: 제외/채택 동작 ─────────────────────────
def _build_notes_doc(*table_xml_chunks: str) -> str:
    """주석 루트 타이틀 아래에 표들이 들어있는 합성 XML."""
    body = "\n".join(table_xml_chunks)
    return f"<document><title>재무제표 주석</title>{body}</document>"


# 자산 변동표 — 흐름 컬럼(기초/취득/처분/대체/기말)으로만 식별되도록 구성한다.
# 헤더에 "누계액"·"취득원가"가 없으므로 _has_exclude_keywords는 통과하고, 제목에
# "유형자산"·"사용권자산" 등 _PARTIAL_TITLE_KEYWORDS도 들어가지 않아야 한다.
# 그래야 제외가 본 명세에서 추가한 분류기 단계에서 일어났음을 확인할 수 있다.
_ASSET_MOVEMENT_TABLE = """
<title>10. 자산 증감</title>
<table>
  <tr><td>구분</td><td>기초</td><td>취득</td><td>처분</td><td>대체</td><td>기말</td></tr>
  <tr><td>감가상각비</td><td>10</td><td>5</td><td>3</td><td>1</td><td>13</td></tr>
</table>
"""

# 기능별 배분표 — 제목에 _PARTIAL_TITLE_KEYWORDS가 들어가지 않으면서 헤더 컬럼만으로
# 분류기가 functional_breakdown을 감지해야 한다.
_FUNCTIONAL_TABLE = """
<title>30. 비용 분류</title>
<table>
  <tr><td>구분</td><td>판매비와관리비</td><td>매출원가</td><td>합계</td></tr>
  <tr><td>감가상각비</td><td>10</td><td>20</td><td>30</td></tr>
</table>
"""

# 비용의 성격별 분류 — 회사 전체 비용 표. 제목이 _get_section_title의 정규식("숫자.")
# 형태여야 트레이스에 제대로 잡힌다(실제 DART 주석 제목 관례).
_EXPENSE_BY_NATURE_TABLE = """
<title>31. 비용의 성격별 분류</title>
<table>
  <tr><td>구분</td><td>당기</td><td>전기</td></tr>
  <tr><td>감가상각비</td><td>100</td><td>90</td></tr>
  <tr><td>무형자산상각비</td><td>10</td><td>9</td></tr>
</table>
"""


class TestCollect:
    def test_excludes_asset_movement_and_functional_breakdown(self):
        # _has_exclude_keywords가 누계액을 잡지만, 잡지 못한 흐름·기능별 케이스도
        # 분류기가 후속 제외해야 한다. 합성 케이스로 분류기 단계 검증.
        xml = _build_notes_doc(
            _ASSET_MOVEMENT_TABLE,
            _FUNCTIONAL_TABLE,
            _EXPENSE_BY_NATURE_TABLE,
        )
        trace: list[str] = []
        # strict_scope=False → 스코프 무시하고 모든 주석 표를 후보로 모은다.
        tables = _collect_depreciation_tables(_soup(xml), strict_scope=False, debug_trace=trace)

        # 자산 변동표/기능별 배분은 후보에서 제외되고 비용 성격별 분류만 남아야 한다.
        candidate_types = [t.get("candidate_type") for t in tables]
        assert "asset_movement" not in candidate_types
        assert "functional_breakdown" not in candidate_types
        assert "expense_by_nature" in candidate_types

    def test_trace_records_exclusion_reason_and_candidate_type(self):
        xml = _build_notes_doc(_ASSET_MOVEMENT_TABLE, _EXPENSE_BY_NATURE_TABLE)
        trace: list[str] = []
        _collect_depreciation_tables(_soup(xml), strict_scope=False, debug_trace=trace)
        joined = "\n".join(trace)

        # 제외 사유 + 채택 + candidate_type이 트레이스에 남아야 한다.
        assert "구조 분류" in joined and "후보 제외" in joined
        assert "후보 채택" in joined
        assert "candidate_type=expense_by_nature" in joined

    def test_surviving_candidate_has_candidate_type_and_risk_flags(self):
        xml = _build_notes_doc(_EXPENSE_BY_NATURE_TABLE)
        tables = _collect_depreciation_tables(_soup(xml), strict_scope=False)
        assert len(tables) == 1
        t = tables[0]
        assert t["candidate_type"] == "expense_by_nature"
        assert t["risk_flags"] == []
        # AI에 전달되는 텍스트에도 candidate_type이 들어가 위치 선택에 활용된다.
        assert "[CANDIDATE_TYPE] expense_by_nature" in t["text"]


# ── risk_flags가 item_details에 전파되어 confidence를 강등하는지 ──────────────
class TestRiskFlagPropagation:
    def test_risk_flags_downgrade_confidence_and_attach(self):
        items = {"감가상각비": 100.0, "사용권자산상각비": None, "무형자산상각비": None}
        details = _build_depreciation_item_details(
            items,
            rcept_no="X",
            fs_div="CFS",
            source="notes",
            risk_flags_by_item={"감가상각비": ["partial_asset_class_title"]},
            selected_candidate_type_by_item={"감가상각비": "general_depreciation"},
        )
        d = details["감가상각비"]
        assert d["value"] == 100.0
        assert d["confidence"] == "low_confidence"
        assert "partial_asset_class_title" in d["flags"]
        assert d["source"]["candidate_type"] == "general_depreciation"

    def test_no_risk_flags_keeps_confidence_for_notes_source(self):
        items = {"감가상각비": 100.0, "사용권자산상각비": None, "무형자산상각비": None}
        details = _build_depreciation_item_details(
            items,
            rcept_no="X",
            fs_div="CFS",
            source="notes",
            risk_flags_by_item={},
            selected_candidate_type_by_item={"감가상각비": "expense_by_nature"},
        )
        d = details["감가상각비"]
        # source != "error" 이고 risk_flags가 없으면 강등되지 않는다.
        assert d["confidence"] != "low_confidence"
        assert d["source"]["candidate_type"] == "expense_by_nature"

    def test_risk_flags_ignored_for_items_without_value(self):
        items = {"감가상각비": None, "사용권자산상각비": None, "무형자산상각비": None}
        details = _build_depreciation_item_details(
            items,
            rcept_no="X",
            fs_div="CFS",
            source="error",
            risk_flags_by_item={"감가상각비": ["functional_breakdown_columns"]},
        )
        d = details["감가상각비"]
        # 값이 없는 항목에는 risk_flags를 부착하지 않는다 (잘못된 신호 방지).
        assert "functional_breakdown_columns" not in d["flags"]
