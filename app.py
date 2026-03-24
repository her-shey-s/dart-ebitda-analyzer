"""
app.py
DART EBITDA Analyzer - Streamlit 메인 UI

외감기업(비상장 포함)의 재무 데이터를 자동 추출하는 웹 앱.
사용자가 기업명과 사업연도를 입력하면:
  경로A (사업보고서): DART 재무제표 API 직접 호출
  경로B (감사보고서): 원문 HTML 파싱 → 실패 시 Gemini Flash 폴백
3단계 검증 후 결과를 표·차트·엑셀로 출력한다.
"""

import streamlit as st
import pandas as pd

from config import FINANCIAL_ITEMS, REPORT_CODES
from dart_api.corp_search import get_corp_code, search_corp_by_name
from dart_api.report_finder import get_latest_report, get_document_index, find_financial_statement_doc
from dart_api.financial_api import get_financial_data_path_a, fetch_major_accounts, extract_target_items
from dart_api.html_parser import download_html, parse_financial_data_from_html, is_parse_successful
from ai_module.gemini_parser import extract_financials_with_gemini, validate_with_gemini
from validator.rules import validate_accounting_identities, cross_validate, summarize_validation
from utils.cache import make_cache_key, get_cache, set_cache, purge_expired

# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DART 재무데이터 추출기",
    page_icon="📊",
    layout="wide",
)

# ── 사이드바: 설정 ────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("설정")
    year = st.selectbox("사업연도", options=list(range(2023, 2018, -1)), index=0)
    prefer_consolidated = st.checkbox("연결재무제표 우선", value=True)
    use_cache = st.checkbox("캐시 사용", value=True)
    if st.button("캐시 초기화"):
        from utils.cache import clear_all_cache
        clear_all_cache()
        st.success("캐시를 초기화했습니다.")

# ── 메인 UI ───────────────────────────────────────────────────────────────────
st.title("📊 DART 재무데이터 추출기")
st.caption("외감기업(비상장 포함) 재무제표 자동 추출 · 검증 시스템")

col1, col2 = st.columns([3, 1])
with col1:
    corp_name_input = st.text_input("기업명 입력", placeholder="예: 삼성전자, (주)○○")
with col2:
    search_btn = st.button("검색", type="primary", use_container_width=True)


def run_extraction(corp_name: str, year: int) -> None:
    """
    기업명과 연도를 받아 재무 데이터 추출 전체 파이프라인을 실행한다.

    Args:
        corp_name: 사용자 입력 기업명
        year:      사업연도
    """
    # ── 1. corp_code 조회 ──────────────────────────────────────────────────
    with st.spinner("기업 코드 조회 중..."):
        matches = search_corp_by_name(corp_name)

    if matches.empty:
        st.error(f"'{corp_name}'에 해당하는 기업을 찾을 수 없습니다.")
        return

    # 복수 결과 → 선택 UI
    if len(matches) > 1:
        options = matches["corp_name"].tolist()
        selected = st.selectbox("기업을 선택하세요", options=options)
        corp_code = matches.loc[matches["corp_name"] == selected, "corp_code"].iloc[0]
        corp_name = selected
    else:
        corp_code = matches.iloc[0]["corp_code"]
        corp_name = matches.iloc[0]["corp_name"]

    st.info(f"**{corp_name}** (corp_code: `{corp_code}`) · {year}년 데이터 추출")

    # ── 2. 캐시 확인 ───────────────────────────────────────────────────────
    cache_key = make_cache_key(corp_code, str(year))
    if use_cache:
        cached = get_cache(cache_key)
        if cached:
            st.success("캐시에서 불러왔습니다.")
            _display_result(cached)
            return

    # ── 3. 보고서 탐색 ─────────────────────────────────────────────────────
    with st.spinner("보고서 탐색 중..."):
        report_meta = get_latest_report(corp_code, year)

    if report_meta is None:
        st.error(f"{year}년 보고서를 찾을 수 없습니다.")
        return

    st.write(f"보고서: **{report_meta['report_nm']}** (접수일: {report_meta['rcept_dt']})")

    # ── 4. 재무 데이터 추출 (경로 분기) ───────────────────────────────────
    items: dict = {}
    path_used: str = ""
    raw_list = []

    if report_meta["report_type"] == "annual":
        # 경로A
        with st.spinner("재무제표 API 호출 중 (경로A)..."):
            result_a = get_financial_data_path_a(
                corp_code, year, REPORT_CODES["annual"]
            )
        items = result_a["items"]
        raw_list = result_a["raw_list"]
        path_used = f"경로A (재무제표 API, {result_a['fs_div']})"

    else:
        # 경로B: HTML 파싱
        with st.spinner("감사보고서 원문 다운로드 중 (경로B)..."):
            doc_index = get_document_index(report_meta["rcept_no"])
            html_url = find_financial_statement_doc(doc_index)

        if not html_url:
            st.error("재무제표 문서 URL을 찾을 수 없습니다.")
            return

        with st.spinner("HTML 파싱 중..."):
            html = download_html(html_url)
            if html:
                items = parse_financial_data_from_html(html)

        if not is_parse_successful(items):
            st.warning("HTML 파싱 일부 실패 → Gemini Flash로 재추출 중...")
            table_text = html[:12000] if html else ""
            items = extract_financials_with_gemini(table_text, corp_name, year)
            path_used = "경로B (Gemini Flash 폴백)"
        else:
            path_used = "경로B (HTML 파싱)"

    st.caption(f"데이터 수집 경로: {path_used}")

    # ── 5. 검증 ───────────────────────────────────────────────────────────
    identity_flags = validate_accounting_identities(items)
    cross_flags = []

    if report_meta["report_type"] == "annual" and raw_list:
        with st.spinner("교차 검증 중..."):
            try:
                major_raw = fetch_major_accounts(
                    corp_code, str(year), REPORT_CODES["annual"]
                )
                major_items = extract_target_items(major_raw, str(year))
                cross_flags = cross_validate(items, major_items)
            except Exception:
                pass  # 교차검증 실패는 무시

    validation = summarize_validation(identity_flags, cross_flags)

    ai_validation = None
    if validation["needs_ai"]:
        with st.spinner("AI 검증 중 (Gemini Flash)..."):
            try:
                ai_validation = validate_with_gemini(
                    items, validation["all_flags"], corp_name, year
                )
            except Exception as e:
                st.warning(f"AI 검증 실패: {e}")

    # ── 6. 결과 패키징 및 캐싱 ────────────────────────────────────────────
    output = {
        "corp_name":    corp_name,
        "corp_code":    corp_code,
        "year":         year,
        "path_used":    path_used,
        "items":        items,
        "validation":   validation,
        "ai_validation":ai_validation,
    }
    if use_cache:
        set_cache(cache_key, output, ttl_hours=48)

    _display_result(output)


def _display_result(output: dict) -> None:
    """
    추출 결과를 Streamlit UI에 렌더링한다.

    Args:
        output: run_extraction()에서 생성된 결과 딕셔너리
    """
    items = output["items"]
    validation = output["validation"]
    ai_val = output.get("ai_validation")

    st.divider()
    st.subheader("재무 데이터")

    # 항목 테이블
    rows = []
    for item in FINANCIAL_ITEMS:
        val = items.get(item["name"])
        rows.append({
            "항목":       item["name"],
            "재무제표":   item["fs_type"],
            "금액 (원)":  f"{val:,.0f}" if val is not None else "N/A",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # 검증 결과
    st.subheader("검증 결과")
    if validation["passed"]:
        st.success("규칙 검증 통과")
    else:
        for flag in validation["all_flags"]:
            st.warning(flag)

    if ai_val:
        if ai_val.get("is_valid"):
            st.success("AI 검증: 이상 없음")
        else:
            for issue in ai_val.get("issues", []):
                st.error(f"AI 검증 이슈: {issue}")

    # 엑셀 다운로드
    df_export = pd.DataFrame([
        {"항목": k, "금액": v} for k, v in items.items()
    ])
    xlsx_bytes = _to_excel(df_export)
    st.download_button(
        label="엑셀 다운로드",
        data=xlsx_bytes,
        file_name=f"{output['corp_name']}_{output['year']}_재무데이터.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _to_excel(df: pd.DataFrame) -> bytes:
    """DataFrame을 엑셀 bytes로 변환한다."""
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="재무데이터")
    return buf.getvalue()


# ── 실행 ──────────────────────────────────────────────────────────────────────
if search_btn and corp_name_input.strip():
    purge_expired()
    run_extraction(corp_name_input.strip(), year)
elif search_btn:
    st.warning("기업명을 입력하세요.")
