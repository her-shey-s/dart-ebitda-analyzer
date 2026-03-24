"""
app.py
DART 재무 데이터 분석기 - Streamlit 메인 UI

여러 기업명과 사업연도를 입력하면 DART API로 재무 데이터를 자동 추출하고
회계 항등식·교차 검증까지 수행하여 결과를 표·상세뷰·엑셀로 출력한다.
"""

import io
from itertools import product
from typing import Optional

import pandas as pd
import streamlit as st

from config import FINANCIAL_ITEMS
from dart_api.corp_search import get_corp_code, search_corp
from dart_api.financial_api import get_financial_data_path_a
from dart_api.html_parser import get_financial_data_path_b
from dart_api.report_finder import find_report
from utils.cache import clear_all_cache, get_cache, make_cache_key, purge_expired, set_cache
from validator.rules import needs_ai_validation, validate

# ── 페이지 설정 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DART 재무 데이터 분석기",
    page_icon="📊",
    layout="wide",
)

# ── 세션 상태 초기화 ──────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results: list[dict] = []

# ── 표시 컬럼 정의 ────────────────────────────────────────────────────────────
# 요약 테이블에 표시할 재무 항목 (순서 유지)
_DISPLAY_ITEMS = [
    "총자산", "총부채", "이익잉여금",
    "매출액", "매출총이익", "영업이익", "당기순이익",
]


# ── 금액 포맷 ─────────────────────────────────────────────────────────────────

def _fmt_억(val: Optional[float]) -> str:
    """원(KRW) 값을 억 단위 문자열로 변환한다. 음수는 괄호 표기."""
    if val is None:
        return "-"
    억 = val / 1e8
    if 억 < 0:
        return f"({abs(억):,.0f})"
    return f"{억:,.0f}"


def _fmt_억_raw(val: Optional[float]) -> Optional[float]:
    """Excel 출력용: 억 단위 float 반환."""
    return round(val / 1e8, 1) if val is not None else None


# ── 단일 기업·연도 분석 ───────────────────────────────────────────────────────

def _analyze_one(corp_name: str, year: int, use_cache: bool) -> dict:
    """
    기업명·연도 1쌍에 대한 전체 파이프라인을 실행하여 결과 딕셔너리를 반환한다.

    반환 키:
        corp_name, year, corp_code, path, report_nm, fs_div,
        items, validation, status, error_msg
    """
    base: dict = {
        "corp_name": corp_name,
        "year":      year,
        "corp_code": "-",
        "path":      "-",
        "report_nm": "-",
        "fs_div":    "-",
        "items":     {item["name"]: None for item in FINANCIAL_ITEMS},
        "validation": None,
        "status":    "ok",
        "error_msg": "",
    }

    # 1. corp_code 조회
    corp_code = get_corp_code(corp_name)
    if corp_code is None:
        # 유사 기업명 힌트 제공
        similar = search_corp(corp_name)
        hint = ""
        if not similar.empty:
            names = similar["corp_name"].head(3).tolist()
            hint = f" (유사: {', '.join(names)})"
        return {**base, "status": "no_corp", "error_msg": f"기업 미발견{hint}"}
    base["corp_code"] = corp_code

    # 2. 캐시 확인
    cache_key = make_cache_key(corp_code, str(year))
    if use_cache:
        cached = get_cache(cache_key)
        if cached:
            return {**cached, "from_cache": True}

    # 3. 보고서 탐색
    try:
        report = find_report(corp_code, year)
    except Exception as e:
        return {**base, "status": "error", "error_msg": f"보고서 탐색 오류: {e}"}

    if report is None:
        return {**base, "status": "no_report", "error_msg": "보고서 없음"}

    base["path"]      = report["path"]
    base["report_nm"] = report["report_nm"]

    # 4. 재무 데이터 추출
    try:
        if report["path"] == "A":
            data = get_financial_data_path_a(corp_code, year, report["reprt_code"])
        else:
            data = get_financial_data_path_b(report["rcept_no"])
    except Exception as e:
        return {**base, "status": "error", "error_msg": f"재무데이터 추출 오류: {e}"}

    if data.get("error"):
        return {**base, "status": "error", "error_msg": data["error"]}

    base["items"]  = data["items"]
    base["fs_div"] = data.get("fs_div", "-")

    # 5. 검증
    try:
        validation = validate(data["items"], data.get("cross_check", {}), path=report["path"])
    except Exception as e:
        validation = {"is_valid": None, "checks": [], "flags": [f"검증 오류: {e}"]}
    base["validation"] = validation

    # 6. 캐시 저장
    if use_cache:
        set_cache(cache_key, base, ttl_hours=48)

    return base


# ── 결과 → DataFrame ──────────────────────────────────────────────────────────

def _to_summary_df(results: list[dict]) -> pd.DataFrame:
    """결과 리스트를 요약 표시용 DataFrame으로 변환한다."""
    rows = []
    for r in results:
        items = r.get("items", {})
        val   = r.get("validation")

        if r["status"] == "ok":
            valid_str = "✓ 통과" if (val and val["is_valid"]) else "✗ 검증실패"
            if val and needs_ai_validation(val):
                valid_str += " ⚠"
        else:
            valid_str = r["error_msg"]

        row = {
            "기업명":   r["corp_name"],
            "연도":     r["year"],
            "경로":     r["path"],
            "보고서":   r["report_nm"],
        }
        for name in _DISPLAY_ITEMS:
            row[f"{name}(억)"] = _fmt_억(items.get(name))
        row["검증"] = valid_str
        rows.append(row)

    return pd.DataFrame(rows)


# ── 결과 → Excel bytes ────────────────────────────────────────────────────────

def _to_excel_bytes(results: list[dict]) -> bytes:
    """결과 리스트를 Excel bytes로 변환한다. 두 시트(요약·원본) 포함."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # 시트1: 요약 (억 단위)
        _to_summary_df(results).to_excel(writer, sheet_name="요약", index=False)

        # 시트2: 원본 금액 (원 단위)
        raw_rows = []
        for r in results:
            items = r.get("items", {})
            row = {"기업명": r["corp_name"], "연도": r["year"], "경로": r["path"]}
            for item in FINANCIAL_ITEMS:
                row[item["name"]] = items.get(item["name"])
            raw_rows.append(row)
        pd.DataFrame(raw_rows).to_excel(writer, sheet_name="원본(원단위)", index=False)

    return buf.getvalue()


# ── 검증 상세 렌더링 ──────────────────────────────────────────────────────────

def _render_validation_detail(result: dict) -> None:
    """단일 결과의 검증 상세 내역을 Streamlit으로 렌더링한다."""
    if result["status"] != "ok" or result["validation"] is None:
        st.warning(result["error_msg"] or "데이터 없음")
        return

    val = result["validation"]

    # 검증 항목 테이블
    rows = []
    for c in val["checks"]:
        sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(c["severity"], "⚪")
        passed_icon = "✓" if c["passed"] else "✗"
        exp = c.get("expected")
        act = c.get("actual")
        diff = c.get("diff")

        rows.append({
            "":       f"{passed_icon} {sev_icon}",
            "규칙":   c["rule"],
            "기대값": f"{exp:,.0f}" if isinstance(exp, (int, float)) else (str(exp) if exp else "-"),
            "실제값": f"{act:,.0f}" if isinstance(act, (int, float)) else (str(act) if act else "-"),
            "차이":   f"{diff:,.0f}원" if isinstance(diff, (int, float)) else "-",
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

    # 플래그
    if val["flags"]:
        st.markdown("**플래그:**")
        for flag in val["flags"]:
            st.warning(flag)
    else:
        st.success("이상 없음")

    # 재무 상세
    items = result.get("items", {})
    if any(v is not None for v in items.values()):
        st.markdown("**재무 항목 상세:**")
        detail_rows = []
        for item in FINANCIAL_ITEMS:
            val_raw = items.get(item["name"])
            detail_rows.append({
                "항목":     item["name"],
                "구분":     item["fs_type"],
                "금액(억)": _fmt_억(val_raw),
                "금액(원)": f"{val_raw:,.0f}" if val_raw is not None else "-",
            })
        st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)


# ── 사이드바 ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📊 DART 재무 데이터 분석기")
    st.caption("외감기업(비상장 포함) 재무제표 자동 추출 · 검증")
    st.divider()

    corp_input = st.text_area(
        "기업명 입력",
        placeholder="삼성전자\n한국맥도날드\n카카오\n(줄바꿈으로 여러 기업 입력, 최대 100개)",
        height=180,
    )

    years_selected = st.multiselect(
        "분석 연도",
        options=list(range(2024, 2019, -1)),
        default=[2023],
    )

    use_cache = st.checkbox("캐시 사용", value=True)

    analyze_btn = st.button("🔍 분석 시작", type="primary", use_container_width=True)

    st.divider()
    if st.button("캐시 초기화", use_container_width=True):
        clear_all_cache()
        st.session_state.results = []
        st.success("캐시 초기화 완료")


# ── 메인 영역 ─────────────────────────────────────────────────────────────────

st.header("DART 재무 데이터 분석기")

# ── 분석 실행 ─────────────────────────────────────────────────────────────────
if analyze_btn:
    corps = [c.strip() for c in corp_input.splitlines() if c.strip()][:100]
    if not corps:
        st.warning("기업명을 입력하세요.")
    elif not years_selected:
        st.warning("분석 연도를 선택하세요.")
    else:
        purge_expired()
        tasks = list(product(corps, sorted(years_selected)))
        total = len(tasks)

        progress_bar = st.progress(0, text="분석 준비 중...")
        status_text  = st.empty()

        results: list[dict] = []
        for i, (corp_name, year) in enumerate(tasks):
            status_text.markdown(f"**처리 중** ({i + 1}/{total}): `{corp_name}` {year}년")
            result = _analyze_one(corp_name, year, use_cache)
            results.append(result)
            progress_bar.progress((i + 1) / total, text=f"{i + 1}/{total} 완료")

        progress_bar.empty()
        status_text.empty()
        st.session_state.results = results
        st.success(f"✓ {total}건 분석 완료 (성공: {sum(1 for r in results if r['status'] == 'ok')}건)")

# ── 결과 표시 ─────────────────────────────────────────────────────────────────
if st.session_state.results:
    results = st.session_state.results
    tab_summary, tab_detail = st.tabs(["📋 요약", "🔍 상세"])

    # ── 탭1: 요약 ─────────────────────────────────────────────────────────────
    with tab_summary:
        df_summary = _to_summary_df(results)

        # 검증 실패 행 강조
        def _highlight_row(row):
            if "✗" in str(row.get("검증", "")):
                return ["background-color: #fff0f0"] * len(row)
            if "미발견" in str(row.get("검증", "")) or "없음" in str(row.get("검증", "")):
                return ["background-color: #fffff0"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df_summary.style.apply(_highlight_row, axis=1),
            use_container_width=True,
            hide_index=True,
            height=min(60 + len(df_summary) * 35, 600),
        )

        # 집계 요약
        ok_results = [r for r in results if r["status"] == "ok"]
        if ok_results:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("총 분석 건수", f"{len(results)}건")
            col2.metric("데이터 추출 성공", f"{len(ok_results)}건")
            col3.metric("검증 통과",  f"{sum(1 for r in ok_results if r['validation'] and r['validation']['is_valid'])}건")
            col4.metric("검증 실패",  f"{sum(1 for r in ok_results if r['validation'] and not r['validation']['is_valid'])}건")

    # ── 탭2: 상세 ─────────────────────────────────────────────────────────────
    with tab_detail:
        for r in results:
            label_icon = "✓" if r["status"] == "ok" and r["validation"] and r["validation"]["is_valid"] else "✗"
            label = f"{label_icon} {r['corp_name']} · {r['year']}년 · {r['path']} · {r['report_nm']}"
            with st.expander(label):
                _render_validation_detail(r)

    # ── 다운로드 ──────────────────────────────────────────────────────────────
    st.divider()
    xlsx = _to_excel_bytes(results)
    st.download_button(
        label="📥 엑셀 다운로드 (요약 + 원본)",
        data=xlsx,
        file_name="dart_재무데이터.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

else:
    st.info("사이드바에서 기업명과 연도를 입력하고 '분석 시작'을 눌러주세요.")
    st.markdown("""
**사용 방법:**
1. 좌측 사이드바에 분석할 기업명을 한 줄에 하나씩 입력
2. 분석 연도 선택 (복수 선택 가능)
3. '분석 시작' 클릭

**지원 기업:**
- 상장사: 사업보고서 기반 (경로A, DART 재무제표 API)
- 비상장 외감기업: 감사보고서 기반 (경로B, DART XML 파싱)
""")
