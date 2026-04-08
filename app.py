"""
app.py
DART 재무 데이터 분석기 - Streamlit 메인 UI

여러 기업명과 사업연도를 입력하면 DART API로 재무 데이터를 자동 추출하고
회계 항등식·교차 검증까지 수행하여 결과를 표·상세뷰·엑셀로 출력한다.
"""

import io
import re
from itertools import product
from typing import Optional

import pandas as pd
import streamlit as st

from config import FINANCIAL_ITEMS, GEMINI_API_KEY
from dart_api.corp_search import get_corp_code, search_corp
from dart_api.financial_api import get_financial_data_path_a
from dart_api.html_parser import get_financial_data_path_b
from dart_api.report_finder import find_report
from utils.analysis_logger import AnalysisLogger, format_amount
from utils.cache import clear_all_cache, get_cache, make_cache_key, purge_expired, set_cache
from validator.rules import validate

# ── 페이지 설정 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DART 재무 데이터 분석기",
    page_icon="📊",
    layout="wide",
)

# ── 세션 상태 초기화 ──────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results: list[dict] = []
if "analysis_logs" not in st.session_state:
    st.session_state.analysis_logs: list[str] = []

# ── 표시 컬럼 정의 ────────────────────────────────────────────────────────────
# 요약 테이블에 표시할 재무 항목 (순서 유지)
_DISPLAY_ITEMS = [
    "총자산", "총부채", "이익잉여금", "감가상각비", "무형자산상각비",
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
    """Excel 출력용: 억 단위 float 반환 (반올림 없이 원본 정밀도 유지)."""
    return val / 1e8 if val is not None else None


def _sanitize_filename_part(text: str) -> str:
    """파일명에 안전한 문자열만 남긴다."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (text or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned.strip("._") or "trace"


def _depreciation_trace_filename(result: dict) -> str:
    """결과 1건에 대한 감가상각 추적 로그 파일명을 생성한다."""
    corp = _sanitize_filename_part(result.get("corp_name", "corp"))
    year = result.get("year", "year")
    path = result.get("path", "-")
    report = _sanitize_filename_part(result.get("report_nm", "report"))
    return f"{corp}_{year}_경로{path}_{report}_감가상각추적.txt"


def _depreciation_trace_text(result: dict) -> str:
    """결과 1건의 감가상각 추적 로그를 TXT 본문으로 직렬화한다."""
    trace_lines = result.get("depreciation_trace") or []
    header = [
        f"기업명: {result.get('corp_name', '-')}",
        f"연도: {result.get('year', '-')}",
        f"corp_code: {result.get('corp_code', '-')}",
        f"경로: {result.get('path', '-')}",
        f"보고서: {result.get('report_nm', '-')}",
        f"재무제표기준: {_fs_label(result)}",
        "",
        "[감가상각 추적 로그]",
    ]
    if trace_lines:
        return "\n".join(header + trace_lines)
    return "\n".join(header + ["로그 없음"])


def _validation_status_label(result: dict, use_icon: bool = True) -> str:
    """결과 딕셔너리의 검증 상태를 짧은 라벨로 반환한다."""
    val = result.get("validation")
    if result["status"] != "ok":
        return result["error_msg"]
    if not val:
        return "검증정보 없음"

    if val["is_valid"]:
        label = "통과"
        return f"✓ {label}" if use_icon else label

    label = "검증실패"
    return f"✗ {label}" if use_icon else label


def _processing_status_detail(result: dict) -> str:
    """분석/추출 단계 상태를 사람이 읽기 쉬운 설명으로 반환한다."""
    status = result.get("status")
    if status == "ok":
        report_nm = result.get("report_nm") or "-"
        return (
            f"데이터 추출 성공. 사용 보고서: {report_nm}, "
            f"경로 {result.get('path', '-')}, 재무제표 기준 {_fs_label(result)}."
        )

    if status == "no_corp":
        msg = result.get("error_msg") or "기업명을 찾지 못했습니다."
        if "유사:" in msg:
            return (
                f"{msg}. 입력한 이름과 정확히 일치하는 DART 기업명이 없어 자동 선택하지 않았습니다. "
                "동명이인이나 유사 상호가 많을 수 있으니 정식 법인명으로 다시 조회해 주세요."
            )
        return (
            f"{msg}. DART 기업코드 목록에서 정확 일치 기업명을 찾지 못했습니다."
        )

    if status == "ambiguous_corp":
        msg = result.get("error_msg") or "동일 기업명이 여러 개 존재합니다."
        return f"{msg} DART에 같은 이름의 법인이 다수 등록되어 있어 재무데이터를 특정할 수 없습니다."

    if status == "no_report":
        year = result.get("year")
        return (
            f"{year} 사업연도 기준 사업보고서, 연결감사보고서, 감사보고서를 순서대로 찾았지만 "
            "사용 가능한 보고서를 찾지 못했습니다."
        )

    if status == "error":
        msg = result.get("error_msg") or "분석 중 오류가 발생했습니다."
        if "보고서 탐색 오류" in msg:
            return f"{msg}. DART 공시 목록 조회 단계에서 실패했습니다."
        if "재무데이터 추출 오류" in msg:
            return f"{msg}. 보고서는 찾았지만 재무 항목 추출 단계에서 실패했습니다."
        if "document.xml 다운로드 실패" in msg:
            return f"{msg}. 감사보고서 원문(document.xml)을 내려받지 못했습니다."
        return msg

    return result.get("error_msg") or "상세 사유 정보가 없습니다."


def _validation_reason_lines(result: dict, include_ai: bool = True) -> list[str]:
    """결과 딕셔너리의 검증/처리 사유를 상세 문장 리스트로 반환한다."""
    if result["status"] != "ok":
        return [_processing_status_detail(result)]

    val = result.get("validation")
    if not val:
        return ["검증 정보가 없습니다."]

    lines: list[str] = []
    failed_checks = [c for c in val.get("checks", []) if not c.get("passed")]
    skipped_checks = [c for c in val.get("checks", []) if c.get("passed") and c.get("note")]

    if failed_checks:
        severity_map = {"critical": "치명", "warning": "경고", "info": "참고"}
        for check in failed_checks:
            severity = severity_map.get(check.get("severity"), str(check.get("severity", "")).upper())
            line = f"[{severity}] {check.get('rule', '-')}"
            diff = check.get("diff")
            if isinstance(diff, (int, float)):
                line += f" (차이 {diff:,.0f}원)"
            note = check.get("note")
            if note:
                line += f" - {note}"
            lines.append(line)
    else:
        lines.append("실패한 검증은 없습니다.")

    if skipped_checks:
        for check in skipped_checks:
            lines.append(f"[스킵] {check.get('rule', '-')} - {check.get('note', '사유 없음')}")

    # AI 비교 결과 (경로A·경로B 공통)
    ai_comp = result.get("ai_comparison")
    if include_ai and ai_comp:
        source_label = {
            "agreed": "AI 비교: Python·AI 일치",
            "adjudicated": "AI 비교: AI 판정으로 불일치 해소",
            "ai_extract_only": "AI 비교: AI 추출값 채택",
            "python_fallback": "AI 비교: AI 실패로 Python 결과 사용",
        }.get(ai_comp.get("source"), f"AI 비교: {ai_comp.get('source', '비교 수행')}")
        if ai_comp.get("disagreements"):
            source_label += f" (불일치 항목: {', '.join(ai_comp['disagreements'].keys())})"
        if ai_comp.get("error"):
            source_label += f" / AI 오류: {ai_comp['error']}"
        lines.append(source_label)

    return lines


def _format_validation_detail(result: dict, include_ai: bool = True) -> str:
    """결과 딕셔너리의 검증 상세를 한 줄 문자열로 반환한다."""
    return " | ".join(_validation_reason_lines(result, include_ai=include_ai))


# ── 단일 기업·연도 분석 ───────────────────────────────────────────────────────

def _analyze_one(corp_name: str, year: int, use_cache: bool) -> dict:
    """
    기업명·연도 1쌍에 대한 전체 파이프라인을 실행하여 결과 딕셔너리를 반환한다.

    반환 키:
        corp_name, year, corp_code, path, report_nm, report_type, fs_div,
        items, validation, status, error_msg, analysis_log
    """
    logger = AnalysisLogger(corp_name, year)
    log = logger.log

    base: dict = {
        "corp_name":     corp_name,
        "year":          year,
        "corp_code":     "-",
        "rcept_no":      "-",
        "path":          "-",
        "report_nm":     "-",
        "report_type":   "-",
        "fs_div":        "-",
        "items":         {item["name"]: None for item in FINANCIAL_ITEMS},
        "validation":    None,
        "ai_comparison": None,
        "depreciation_trace": [],
        "status":        "ok",
        "error_msg":     "",
    }

    def _finish(result: dict) -> dict:
        """공통 종료 처리: 로그를 result에 추가하고 반환."""
        log("DONE", f"=== {corp_name} {year} 종료 ({logger.elapsed():.3f}초) status={result.get('status')} ===")
        result["analysis_log"] = logger.get_lines()
        return result

    # 1. corp_code 조회 (동일 기업명 다수 시 추출 중단)
    log("CORP", "기업코드 조회 시작")
    all_corps = search_corp(corp_name)
    exact_corps = all_corps[all_corps["corp_name"] == corp_name]

    if exact_corps.empty:
        hint = ""
        if not all_corps.empty:
            names = all_corps["corp_name"].head(3).tolist()
            hint = f" (유사: {', '.join(names)})"
        log("CORP", f"기업 미발견{hint}")
        return _finish({**base, "status": "no_corp", "error_msg": f"기업 미발견{hint}"})

    if len(exact_corps) > 1:
        log("CORP", f"동일 기업명 {len(exact_corps)}개 존재")
        return _finish({
            **base,
            "status": "ambiguous_corp",
            "error_msg": f"동일 기업명 {len(exact_corps)}개 존재 — 재무데이터를 특정할 수 없습니다.",
        })

    corp_code = exact_corps.iloc[0]["corp_code"]
    base["corp_code"] = corp_code
    log("CORP", f"기업코드 발견: {corp_code}")

    # 2. 캐시 확인
    cache_key = make_cache_key(corp_code, str(year))
    if use_cache:
        log("CACHE", "캐시 확인 중...")
        cached = get_cache(cache_key)
        if cached:
            log("CACHE", "캐시 히트 → 캐시 결과 반환")
            return _finish({**cached, "from_cache": True})
        log("CACHE", "캐시 미스")
    else:
        log("CACHE", "캐시 사용 안 함")

    # 3. 보고서 탐색
    log("REPORT", f"보고서 탐색 시작 (corp_code={corp_code}, year={year})")
    try:
        report = find_report(corp_code, year, log_fn=log)
    except Exception as e:
        log("ERROR", f"보고서 탐색 오류: {e}")
        return _finish({**base, "status": "error", "error_msg": f"보고서 탐색 오류: {e}"})

    if report is None:
        log("REPORT", "보고서 없음")
        return _finish({**base, "status": "no_report", "error_msg": "보고서 없음"})

    base["path"]        = report["path"]
    base["rcept_no"]    = report["rcept_no"]
    base["report_nm"]   = report["report_nm"]
    base["report_type"] = report.get("report_type", "-")
    log("REPORT", f"보고서 확정: {report['report_nm']} (path={report['path']}, rcept_no={report['rcept_no']})")

    # 4. 재무 데이터 추출
    try:
        if report["path"] == "A":
            log("DATA_A", f"경로A 재무데이터 추출 시작 (reprt_code={report['reprt_code']})")
            data = get_financial_data_path_a(corp_code, year, report["reprt_code"], log_fn=log)
        else:
            log("DATA_B", f"경로B 재무데이터 추출 시작 (rcept_no={report['rcept_no']})")
            data = get_financial_data_path_b(report["rcept_no"], report_type=report.get("report_type"), log_fn=log)
    except Exception as e:
        log("ERROR", f"재무데이터 추출 오류: {e}")
        return _finish({**base, "status": "error", "error_msg": f"재무데이터 추출 오류: {e}"})

    if data.get("error"):
        log("ERROR", f"재무데이터 추출 에러: {data['error']}")
        return _finish({**base, "status": "error", "error_msg": data["error"]})

    base["items"]  = data["items"]
    base["fs_div"] = data.get("fs_div", "-")
    base["ai_comparison"] = data.get("ai_comparison")  # 경로A·B 공통 AI 비교 결과

    matched = sum(1 for v in base["items"].values() if v is not None)
    path_label = "A" if report["path"] == "A" else "B"
    log(f"DATA_{path_label}", f"재무데이터 추출 완료: fs_div={base['fs_div']}, {matched}개 항목 매칭")

    # 4-B. 전용 감가상각 추출기 적용
    # 감가상각비·무형자산상각비는 본문 AI 비교보다 이 전용 추출기가 최종 기준이 되도록
    # 항상 마지막에 실행한다. 그래야 합산/분리 규칙을 일관되게 적용할 수 있다.
    log("DEPR", "감가상각 추출 시작")
    try:
        from dart_api.notes_parser import extract_depreciation
        depr_result = extract_depreciation(
            report["rcept_no"],
            fs_div=base.get("fs_div", "CFS"),
            strict_scope=(report["path"] == "A"),
        )
        base["depreciation_trace"] = depr_result.get("trace", [])
        depr_items = depr_result.get("items", {})
        for key in ("감가상각비", "무형자산상각비"):
            if depr_items.get(key) is not None:
                base["items"][key] = depr_items[key]
        # "감가상각비 및 무형자산상각비" 합산 항목인 경우 비고 기록
        if depr_result.get("combined"):
            base["items"]["무형자산상각비"] = None
            base["remarks"] = "감가상각비란에 '감가상각비 및 무형자산상각비' 합산액 기입 (원본에서 분리 불가)"
        log("DEPR", (
            f"감가상각 추출 완료: source={depr_result.get('source')}, "
            f"감가상각비={format_amount(depr_items.get('감가상각비'))}, "
            f"무형자산상각비={format_amount(depr_items.get('무형자산상각비'))}, "
            f"combined={depr_result.get('combined')}"
        ))
    except Exception as e:
        log("DEPR", f"감가상각 추출 실패: {e}")

    # 5. 검증 (회계 항등식)
    log("VALIDATE", "검증 시작")
    try:
        validation = validate(base["items"])
    except Exception as e:
        validation = {"is_valid": None, "checks": [], "flags": [f"검증 오류: {e}"]}

    base["validation"] = validation
    if validation.get("is_valid") is not None:
        log("VALIDATE", f"검증 완료: is_valid={validation['is_valid']}")
    else:
        log("VALIDATE", f"검증 완료: 검증정보 없음")

    # 7. 캐시 저장
    if use_cache:
        set_cache(cache_key, base, ttl_hours=48)
        log("CACHE", "캐시 저장 완료")

    return _finish(base)


# ── 결과 → DataFrame ──────────────────────────────────────────────────────────

def _fs_label(r: dict) -> str:
    """결과 딕셔너리에서 재무제표기준(연결/별도)을 반환한다."""
    if r["path"] == "A":
        return "연결" if r.get("fs_div") == "CFS" else "별도"
    # 경로B: report_type으로 판단
    rt = r.get("report_type", "-")
    if rt == "audit_consol":
        return "연결"
    if rt == "audit_separate":
        return "별도"
    return "-"


def _to_summary_df(results: list[dict]) -> pd.DataFrame:
    """결과 리스트를 요약 표시용 DataFrame으로 변환한다."""
    rows = []
    for r in results:
        items = r.get("items", {})

        row = {
            "기업명":       r["corp_name"],
            "연도":         r["year"],
            "재무제표기준": _fs_label(r),
            "경로":         r["path"],
            "보고서":       r["report_nm"],
        }
        for name in _DISPLAY_ITEMS:
            row[f"{name}(억)"] = _fmt_억(items.get(name))
        row["검증"] = _validation_status_label(r, use_icon=True)
        row["처리상세"] = _processing_status_detail(r)
        row["검증상세"] = _format_validation_detail(r)
        rows.append(row)

    return pd.DataFrame(rows)


# ── 결과 → Excel bytes ────────────────────────────────────────────────────────

def _to_excel_summary_df(results: list[dict]) -> pd.DataFrame:
    """엑셀용 요약 DataFrame: 금액 컬럼을 억 단위 숫자(float)로 반환한다."""
    rows = []
    for r in results:
        items = r.get("items", {})

        row = {
            "기업명":       r["corp_name"],
            "연도":         r["year"],
            "재무제표기준": _fs_label(r),
            "경로":         r["path"],
            "보고서":       r["report_nm"],
        }
        for name in _DISPLAY_ITEMS:
            row[f"{name}(억원)"] = _fmt_억_raw(items.get(name))
        row["검증"] = _validation_status_label(r, use_icon=False)
        row["처리상세"] = _processing_status_detail(r)
        row["검증상세"] = _format_validation_detail(r)
        rows.append(row)

    return pd.DataFrame(rows)


def _apply_number_format(ws, df: pd.DataFrame, fmt: str = "#,##0_);(#,##0);-_)") -> None:
    """워크시트에서 숫자형 컬럼에 엑셀 숫자 서식을 적용한다."""
    from openpyxl.styles import numbers as xl_numbers
    numeric_cols = [i + 1 for i, col in enumerate(df.columns) if pd.api.types.is_float_dtype(df[col]) or pd.api.types.is_integer_dtype(df[col])]
    for col_idx in numeric_cols:
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            for cell in row:
                if cell.value is not None:
                    cell.number_format = fmt


def _to_excel_bytes(results: list[dict]) -> bytes:
    """결과 리스트를 Excel bytes로 변환한다.

    시트 구성: 회사별 1시트 (가로=연도, 세로=계정항목) + 원본 시트 1개.
    """
    from openpyxl.styles import Alignment, Font, PatternFill, numbers as xl_numbers

    # 항목 표시 순서: 매출 먼저, 그 다음 BS
    _EXCEL_ITEMS = ["매출액", "매출총이익", "영업이익", "당기순이익",
                    "총자산", "총부채", "이익잉여금", "감가상각비", "무형자산상각비"]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:

        # ── 회사별 시트 ──────────────────────────────────────────────────
        # 결과를 기업별로 그룹핑
        from collections import defaultdict
        corp_results: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            corp_results[r["corp_name"]].append(r)

        for corp_name, corp_data in corp_results.items():
            # 연도 오름차순 정렬
            corp_data.sort(key=lambda x: x["year"])
            years = [r["year"] for r in corp_data]

            # DataFrame 구성: 항목명 | 2022 | 2023 | 2024 ...
            rows = []
            for item_name in _EXCEL_ITEMS:
                row: dict = {"항목": item_name}
                for r in corp_data:
                    yr = r["year"]
                    val = r.get("items", {}).get(item_name)
                    row[str(yr)] = _fmt_억_raw(val)
                rows.append(row)

            df = pd.DataFrame(rows)

            # 시트명은 최대 31자, 특수문자 제거
            sheet_name = corp_name[:28]
            # 중복 시트명 방지
            existing = [s for s in writer.sheets]
            if sheet_name in existing:
                sheet_name = sheet_name[:25] + f"_{len(existing)}"

            df.to_excel(writer, sheet_name=sheet_name, startrow=2, index=False)
            ws = writer.sheets[sheet_name]

            # 헤더 행1: 기업명
            ws.cell(row=1, column=1, value=corp_name).font = Font(bold=True, size=13)

            # 헤더 행2: 각 연도 아래 연결/별도 + 경로 표시
            for col_idx, r in enumerate(corp_data):
                fs = _fs_label(r)
                path = r["path"]
                cell = ws.cell(row=2, column=col_idx + 2,
                               value=f"{fs} (경로{path})")
                cell.font = Font(italic=True, color="666666", size=9)
                cell.alignment = Alignment(horizontal="center")

            # 연도 헤더(행3)는 pandas가 이미 기록 — 가운데 정렬 + 볼드
            for col_idx in range(len(years)):
                cell = ws.cell(row=3, column=col_idx + 2)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center")

            # 데이터 영역 숫자 서식 적용
            for row_idx in range(4, 4 + len(_EXCEL_ITEMS)):
                for col_idx in range(2, 2 + len(years)):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    if cell.value is not None:
                        cell.number_format = "#,##0_);(#,##0);-_)"
                        cell.alignment = Alignment(horizontal="right")

            # 항목 컬럼 너비 조정
            ws.column_dimensions["A"].width = 14
            for col_idx in range(len(years)):
                from openpyxl.utils import get_column_letter
                ws.column_dimensions[get_column_letter(col_idx + 2)].width = 16

            # 단위 표기
            ws.cell(row=2, column=1, value="(단위: 억원)").font = Font(
                italic=True, color="999999", size=9)

        # ── 원본 시트 (원 단위) ───────────────────────────────────────
        raw_rows = []
        for r in results:
            items = r.get("items", {})
            ai_comp = r.get("ai_comparison")

            row = {
                "기업명":       r["corp_name"],
                "연도":         r["year"],
                "corp_code":   r.get("corp_code", "-"),
                "재무제표기준": _fs_label(r),
                "경로":         r["path"],
                "보고서":       r.get("report_nm", "-"),
                "처리상태":     r["status"],
                "처리상세":     _processing_status_detail(r),
                "검증":         _validation_status_label(r, use_icon=False),
                "검증상세":     _format_validation_detail(r),
                "AI비교결과":   (ai_comp or {}).get("source", ""),
                "AI불일치항목": ", ".join((ai_comp or {}).get("disagreements", {}).keys()),
            }
            for item in FINANCIAL_ITEMS:
                row[item["name"]] = items.get(item["name"])
            row["비고"] = r.get("remarks", "")
            row["감가상각추적라인수"] = len(r.get("depreciation_trace", []) or [])
            row["감가상각추적TXT"] = (
                _depreciation_trace_filename(r)
                if r.get("depreciation_trace")
                else ""
            )
            raw_rows.append(row)
        df_raw = pd.DataFrame(raw_rows)
        df_raw.to_excel(writer, sheet_name="원본(원단위)", index=False)
        ws_raw = writer.sheets["원본(원단위)"]
        _apply_number_format(ws_raw, df_raw)
        ws_raw.column_dimensions["A"].width = 16
        ws_raw.column_dimensions["B"].width = 10
        ws_raw.column_dimensions["C"].width = 12
        ws_raw.column_dimensions["D"].width = 14
        ws_raw.column_dimensions["E"].width = 8
        ws_raw.column_dimensions["F"].width = 24
        ws_raw.column_dimensions["G"].width = 12
        ws_raw.column_dimensions["H"].width = 55
        ws_raw.column_dimensions["I"].width = 22
        ws_raw.column_dimensions["J"].width = 90
        ws_raw.column_dimensions["K"].width = 26
        for row in ws_raw.iter_rows(min_row=2, min_col=6, max_col=10):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    return buf.getvalue()


# ── 검증 상세 렌더링 ──────────────────────────────────────────────────────────

def _render_validation_detail(result: dict) -> None:
    """단일 결과의 검증 상세 내역을 Streamlit으로 렌더링한다."""
    if result["status"] != "ok" or result["validation"] is None:
        st.warning(_processing_status_detail(result))
        return

    val = result["validation"]
    reason_lines = _validation_reason_lines(result)

    st.markdown("**처리 상태:**")
    st.info(_processing_status_detail(result))

    st.markdown("**검증 요약:**")
    for line in reason_lines:
        st.write(f"- {line}")

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

    # AI 비교 결과 (경로A·B 공통)
    ai_comp = result.get("ai_comparison")
    if ai_comp:
        source = ai_comp.get("source", "unknown")
        ai_calls = ai_comp.get("ai_calls", 0)
        source_label = {
            "agreed":          "✅ Python·AI 일치",
            "adjudicated":     "⚖️ AI 판정 (불일치 해소)",
            "ai_extract_only": "🤖 AI 추출 (판정 실패)",
            "python_fallback":  "🐍 Python만 (AI 실패)",
        }.get(source, source)
        st.markdown(f"**AI 비교 결과:** {source_label} (AI 호출: {ai_calls}회)")

        # 불일치 내역 표시
        disagreements = ai_comp.get("disagreements", {})
        if disagreements:
            comp_rows = []
            for name, (py_val, ai_val) in disagreements.items():
                final_val = result.get("items", {}).get(name)
                comp_rows.append({
                    "항목":         name,
                    "Python(원)":   f"{py_val:,.0f}" if py_val is not None else "-",
                    "AI(원)":       f"{ai_val:,.0f}" if ai_val is not None else "-",
                    "최종 채택(원)": f"{final_val:,.0f}" if final_val is not None else "-",
                })
            st.dataframe(pd.DataFrame(comp_rows), hide_index=True, use_container_width=True)

        if ai_comp.get("error"):
            st.warning(f"AI 오류: {ai_comp['error']}")

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

    trace_lines = result.get("depreciation_trace") or []
    if trace_lines:
        st.markdown("**감가상각 추적 로그:**")
        st.download_button(
            label="감가상각 추적 로그 다운로드 (.txt)",
            data=_depreciation_trace_text(result),
            file_name=_depreciation_trace_filename(result),
            mime="text/plain",
            key=f"depr_trace_{result.get('corp_code', '-')}_{result.get('year', '-')}_{result.get('rcept_no', '-')}",
        )
        st.code("\n".join(trace_lines), language="text")


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

    year_options = list(range(2020, 2026))  # 2020~2025
    col_y1, col_y2 = st.columns(2)
    with col_y1:
        year_start = st.selectbox("시작 연도", options=year_options, index=2)  # 기본 2022
    with col_y2:
        year_end = st.selectbox("끝 연도", options=year_options, index=5)      # 기본 2025
    years_selected = list(range(year_start, year_end + 1)) if year_start <= year_end else []

    use_cache = st.checkbox("캐시 사용", value=True)

    analyze_btn = st.button("🔍 분석 시작", type="primary", use_container_width=True)

    st.divider()
    if st.button("캐시 초기화", use_container_width=True):
        clear_all_cache()
        st.session_state.results = []
        st.session_state.analysis_logs = []
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
        all_logs: list[str] = []
        for i, (corp_name, year) in enumerate(tasks):
            status_text.markdown(f"**처리 중** ({i + 1}/{total}): `{corp_name}` {year}년")
            result = _analyze_one(corp_name, year, use_cache)
            all_logs.extend(result.get("analysis_log", []))
            all_logs.append("")  # 기업/연도 사이 빈 줄 구분
            results.append(result)
            progress_bar.progress((i + 1) / total, text=f"{i + 1}/{total} 완료")

        progress_bar.empty()
        status_text.empty()
        st.session_state.results = results
        st.session_state.analysis_logs = all_logs
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
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        xlsx = _to_excel_bytes(results)
        st.download_button(
            label="📥 엑셀 다운로드 (요약 + 원본)",
            data=xlsx,
            file_name="dart_재무데이터.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with col_dl2:
        if st.session_state.get("analysis_logs"):
            from datetime import datetime
            log_text = "\n".join(st.session_state.analysis_logs)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                label="📋 분석 로그 다운로드 (.txt)",
                data=log_text,
                file_name=f"{ts}_dart_분석로그.txt",
                mime="text/plain",
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
