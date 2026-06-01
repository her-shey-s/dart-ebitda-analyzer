"""
pipeline.py
DART 재무 데이터 분석 파이프라인 오케스트레이션

단일 기업·연도에 대한 전체 파이프라인을 실행한다:
  1. 기업코드 조회  2. 보고서 탐색  3. 재무 데이터 추출
  4. 감가상각 추출  5. 검증
"""

from config import FINANCIAL_ITEMS
from dart_api.corp_search import search_corp
from dart_api.report_finder import find_report
from financial.api_extractor import get_financial_data_path_a
from financial.doc_extractor import get_financial_data_path_b
from financial.extraction_result import (
    annotate_report_source,
    details_from_items,
    empty_item_details,
    make_item_detail,
)
from depreciation.extractor import extract_depreciation
from utils.analysis_logger import AnalysisLogger, format_amount
from validator.rules import validate


def analyze_one(
    corp_name: str,
    year: int,
    corp_code_override: str | None = None,
) -> dict:
    """
    기업명·연도 1쌍에 대한 전체 파이프라인을 실행하여 결과 딕셔너리를 반환한다.

    corp_code_override가 지정되면 기업명 검색을 건너뛰고 해당 코드를 사용한다
    (동명 법인 중 사용자가 특정 회사를 선택한 경우).

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
        "item_details":  empty_item_details([item["name"] for item in FINANCIAL_ITEMS]),
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

    # 1. corp_code 조회 (override가 지정되면 검색 스킵)
    if corp_code_override:
        corp_code = corp_code_override
        base["corp_code"] = corp_code
        log("CORP", f"corp_code 지정됨: {corp_code} (사용자가 동명 법인 중 선택)")
    else:
        log("CORP", "기업코드 조회 시작")
        try:
            all_corps = search_corp(corp_name)
        except Exception as e:
            log("CORP", f"기업코드 조회 실패: {e}")
            return _finish({
                **base,
                "status": "error",
                "error_msg": f"DART API 연결 실패 — 잠시 후 다시 시도해 주세요. ({type(e).__name__})",
            })
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

    # 2. 보고서 탐색
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
        # 경로A의 XBRL API(전체재무제표)가 데이터 없음(status=013)을 반환하는 경우가 있다.
        # 사업보고서 본문(document.xml)에는 표가 들어있을 수 있으므로
        # 같은 rcept_no로 본문 파싱(경로B 메소드)을 한 번 더 시도한다.
        if report["path"] == "A":
            original_error = data["error"]
            log("DATA_A", f"경로A XBRL 없음 → 사업보고서 본문 파싱으로 fallback ({original_error})")
            try:
                data = get_financial_data_path_b(
                    report["rcept_no"],
                    report_type=report.get("report_type"),
                    log_fn=log,
                )
            except Exception as e:
                log("ERROR", f"경로A → 본문 fallback 실패: {e}")
                return _finish({
                    **base,
                    "status": "error",
                    "error_msg": f"재무데이터 추출 오류 (경로A·본문 fallback 모두 실패): {e}",
                })
            if data.get("error"):
                log("ERROR", f"경로A·본문 fallback 모두 실패: {data['error']}")
                return _finish({
                    **base,
                    "status": "error",
                    "error_msg": f"{original_error} / 본문 fallback: {data['error']}",
                })
            base["remarks"] = (
                (base.get("remarks") or "")
                + " 사업보고서 본문 파싱으로 추출 (XBRL 미등록)"
            ).strip()
            log("DATA_A", "경로A → 본문 fallback 성공")
        else:
            log("ERROR", f"재무데이터 추출 에러: {data['error']}")
            return _finish({**base, "status": "error", "error_msg": data["error"]})

    base["items"]  = data["items"]
    base["item_details"] = data.get("item_details") or details_from_items(
        data["items"],
        source={"source_type": "legacy_numeric_extractor"},
        confidence="unverified_legacy_value",
        flags=["missing_item_details"],
    )
    annotate_report_source(
        base["item_details"],
        rcept_no=report["rcept_no"],
        report_nm=report["report_nm"],
        path=report["path"],
        report_type=report.get("report_type", "-"),
    )
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
        depr_result = extract_depreciation(
            report["rcept_no"],
            fs_div=base.get("fs_div", "CFS"),
            strict_scope=(report["path"] == "A"),
            log_fn=log,
        )
        base["depreciation_trace"] = depr_result.get("trace", [])
        depr_items = depr_result.get("items", {})
        depr_details = depr_result.get("item_details") or details_from_items(
            depr_items,
            source={
                "source_type": "depreciation_extractor",
                "rcept_no": report["rcept_no"],
                "fs_div": base.get("fs_div", "-"),
                "depreciation_source": depr_result.get("source"),
            },
            confidence="unverified_legacy_value",
            flags=["missing_depreciation_item_details"],
        )
        annotate_report_source(
            depr_details,
            rcept_no=report["rcept_no"],
            report_nm=report["report_nm"],
            path=report["path"],
            report_type=report.get("report_type", "-"),
        )
        for key in ("감가상각비", "사용권자산상각비", "무형자산상각비"):
            if depr_items.get(key) is not None:
                base["items"][key] = depr_items[key]
                if key in depr_details:
                    base["item_details"][key] = depr_details[key]
        # "감가상각비 및 무형자산상각비" 합산 항목인 경우 비고 기록
        if depr_result.get("combined"):
            base["items"]["무형자산상각비"] = None
            base["items"]["사용권자산상각비"] = None
            for combined_key in ("무형자산상각비", "사용권자산상각비"):
                base["item_details"][combined_key] = make_item_detail(
                    combined_key,
                    value=None,
                    raw_value=None,
                    unit_multiplier=None,
                    source={
                        "source_type": "depreciation_extractor",
                        "rcept_no": report["rcept_no"],
                        "report_nm": report["report_nm"],
                        "fs_div": base.get("fs_div", "-"),
                        "depreciation_source": depr_result.get("source"),
                    },
                    confidence="not_separately_disclosed",
                    value_state="not_separately_disclosed",
                    flags=["combined_depreciation_and_amortization"],
                )
            combined_label = depr_result.get("combined_label") or "감가상각비 및 무형자산상각비"
            base["remarks"] = f"감가상각비란에 '{combined_label}' 합산액 기입 (원본에서 분리 불가)"
        log("DEPR", (
            f"감가상각 추출 완료: source={depr_result.get('source')}, "
            f"감가상각비={format_amount(depr_items.get('감가상각비'))}, "
            f"사용권자산상각비={format_amount(depr_items.get('사용권자산상각비'))}, "
            f"무형자산상각비={format_amount(depr_items.get('무형자산상각비'))}, "
            f"combined={depr_result.get('combined')}"
        ))
    except Exception as e:
        log("DEPR", f"감가상각 추출 실패: {e}")

    # 4-C. 비용 항목 절댓값 보정
    # 일부 보고서는 비용을 음수로 표기하므로 항상 양수로 통일한다.
    for cost_key in ("매출원가", "감가상각비", "사용권자산상각비", "무형자산상각비"):
        v = base["items"].get(cost_key)
        if v is not None and v < 0:
            base["items"][cost_key] = abs(v)
            detail = base.get("item_details", {}).get(cost_key)
            if isinstance(detail, dict):
                detail["value"] = abs(v)
                flags = detail.setdefault("flags", [])
                if "negative_cost_normalized_to_positive" not in flags:
                    flags.append("negative_cost_normalized_to_positive")
                detail["normalization"] = {
                    "from": v,
                    "to": abs(v),
                    "reason": "cost_item_positive_display",
                }
            log("NORMALIZE", f"{cost_key}: 음수→양수 보정 ({v} → {abs(v)})")

    # 5. 검증 (회계 항등식)
    log("VALIDATE", "검증 시작")
    try:
        validation = validate(base["items"])
    except Exception as e:
        validation = {
            "is_valid": None,
            "validation_status": "unverified",
            "checks": [],
            "flags": [f"검증 오류: {e}"],
            "skipped": [],
        }

    base["validation"] = validation
    if validation.get("is_valid") is not None:
        log("VALIDATE", f"검증 완료: is_valid={validation['is_valid']}")
    else:
        log("VALIDATE", f"검증 완료: 검증정보 없음")

    return _finish(base)
