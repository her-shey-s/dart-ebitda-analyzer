"""
tests/test_cf_adjustment_notes.py
명세 #7 실패 누적 — 주석 '영업으로부터 창출된 현금' (간접법 산출 내역) 추출 회귀

회귀 케이스 (현대비앤지스틸 사업보고서):
  - 본문 CF에 감가상각비 행이 합산되어 빠지고, 주석에서만 분리 기재되는 회사.
  - 2025년은 "34. 영업으로부터 창출된 현금 (연결)"이 <TITLE>로 잡혀 있고,
    2022년은 같은 표가 <P> 본문에 묻혀 있어 <TITLE>이 없다.
  - 두 경우 모두 content-based로 식별해 CF Stage 1에서 처리해야 한다.
  - '투자부동산상각비'는 라벨에 '감가'가 빠진 형태로도 등장하므로 감가상각비
    버킷에 함께 합산되어야 한다.
"""

from depreciation.extractor import (
    _classify_depreciation_candidate,
    _table_looks_like_cf,
    _table_looks_like_cf_adjustment,
)


# ── CF-조정 판별기 ────────────────────────────────────────────────────────────
class TestTableLooksLikeCfAdjustment:
    def test_2025_style_with_조정_suffix(self):
        # "34. 영업으로부터 창출된 현금 (연결)" 형 — '~에 대한 조정' 라벨
        rows = [
            ["", "공시금액"],
            ["법인세차감전순이익", "23,913,271,893"],
            ["당기순이익조정을 위한 가감", "23,753,966,686"],
            ["이자비용에 대한 조정", "4,594,153,964"],
            ["감가상각비에 대한 조정", "14,588,623,279"],
            ["투자부동산감가상각비 조정", "191,641,141"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is True

    def test_2022_style_short_labels(self):
        # 2022 현대비앤지스틸 형 — 라벨에 '비용'이 끼고 plain '감가상각비'.
        rows = [
            ["구  분", "당 기", "전 기"],
            ["법인세비용차감전 순이익", "33,048,654", "96,862,259"],
            ["조정:", "", ""],
            ["이자비용", "7,251,507", "2,733,012"],
            ["감가상각비", "15,885,746", "15,407,675"],
            ["투자부동산상각비", "198,619", "163,369"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is True

    def test_rejects_main_cf_without_depreciation_breakdown(self):
        # 본문 CF가 간접법이라도 감가상각 행이 합산되어 빠진 경우는 CF-조정으로
        # 분류되지 않는다(주석 표를 찾으러 가야 하므로).
        rows = [
            ["구  분", "당 기"],
            ["영업활동현금흐름", "30,000,000"],
            ["영업으로부터 창출된 현금흐름", "28,000,000"],
            ["이자수취", "1,000,000"],
            ["이자지급", "(2,000,000)"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is False

    def test_rejects_asset_movement_table(self):
        # 유형자산 변동표 — 감가상각 행은 있지만 CF-조정 합계성 마커 없음.
        rows = [
            ["구분", "취득원가", "감가상각누계액", "장부금액"],
            ["기초", "100,000", "20,000", "80,000"],
            ["감가상각", "0", "10,000", "(10,000)"],
            ["기말", "100,000", "30,000", "70,000"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is False

    def test_rejects_functional_breakdown(self):
        # 판매비와관리비 / 매출원가 컬럼으로 비용을 배분한 표.
        rows = [
            ["구분", "판매비와관리비", "매출원가", "합계"],
            ["감가상각비", "1,000", "2,000", "3,000"],
            ["무형자산상각비", "100", "200", "300"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is False

    def test_requires_both_markers(self):
        # 차감전순이익 마커만 있고 감가상각이 없으면 거부.
        rows = [
            ["", ""],
            ["법인세차감전순이익", "10,000"],
            ["이자수익", "100"],
        ]
        assert _table_looks_like_cf_adjustment(rows) is False

    def test_main_cf_still_classified_correctly_by_other_helper(self):
        # 주의: CF-조정 판별기가 본문 CF와 겹치지 않아도, 기존 _table_looks_like_cf는
        # 본문 CF를 그대로 잡는다(서로 보완 관계).
        rows = [
            ["구  분", "당 기"],
            ["영업활동현금흐름", "30,000,000"],
        ]
        assert _table_looks_like_cf(rows) is True
        assert _table_looks_like_cf_adjustment(rows) is False


# ── CF-조정 표가 후보 분류기에서 자산변동표·기능별 배분으로 오인되지 않는지 ──
class TestCfAdjustmentNotMisclassified:
    def test_cf_adjustment_table_is_general_depreciation_not_asset_movement(self):
        # 후보 분류기는 CF-조정 표를 자산 변동표/기능별 배분으로 잘못 분류하면 안 된다.
        # (자산변동/기능별 칼럼이 없고 단일 당기 컬럼이라 통과해야 함)
        rows = [
            ["구  분", "당 기"],
            ["법인세비용차감전 순이익", "33,048,654"],
            ["조정:", ""],
            ["감가상각비", "15,885,746"],
            ["투자부동산상각비", "198,619"],
            ["사용권자산상각비", "326,068"],
            ["무형자산상각비", "423,309"],
        ]
        ctype, flags = _classify_depreciation_candidate(rows, title="35. 영업으로부터 창출된 현금")
        # asset_movement/functional_breakdown으로 빠지지 않아야 한다.
        assert ctype not in ("asset_movement", "functional_breakdown")
