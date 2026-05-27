"""
tests/_harness.py
오프라인 회귀 테스트 하네스 (네트워크/AI 없음)

저장된 fixture(원본 입력)를 추출기에 먹여 결정적 결과를 만들고, golden 결과와
비교한다. 같은 코드를 캡처 도구(scripts/capture_fixture.py)도 재사용하므로,
"캡처 시점에 잠근 동작 == 테스트가 검증하는 동작"이 항상 일치한다.

핵심 경계(여기만 가짜로 바꾸면 네트워크 없이 전체 파싱이 돈다):
  - 경로A 재무: financial.api_extractor.fetch_full_financial_statement
  - 경로B 재무: financial.doc_extractor._download_dart_document
  - 감가상각:   depreciation.extractor._download_all_dart_documents
  - AI 게이트:  config.get_gemini_api_key (빈 문자열 → 순수 파이썬 추출 = 결정적)
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from config import FINANCIAL_ITEMS, FS_DIV
from financial.ebitda import (
    EBITDA_ADDBACKS,
    compute_ebitda,
)

_ITEM_NAMES = [it["name"] for it in FINANCIAL_ITEMS]
_DEPR_KEYS = ("감가상각비", "사용권자산상각비", "무형자산상각비")
# 비용 항목은 파이프라인에서 항상 양수로 보정된다(pipeline.py 4-C).
_COST_KEYS = ("매출원가", "감가상각비", "사용권자산상각비", "무형자산상각비")


# ── 최종 항목 병합 (pipeline.py 4-B/4-C를 그대로 미러링) ──────────────────────
def merge_final_items(financials: dict, depr: Optional[dict]) -> dict:
    """재무 항목에 감가상각 추출 결과를 덮어쓰고 합산/부호 규칙을 적용한다."""
    final = dict(financials)
    if depr:
        depr_items = depr.get("items", {})
        for k in _DEPR_KEYS:
            if depr_items.get(k) is not None:
                final[k] = depr_items[k]
        if depr.get("combined"):
            # "감가상각비 및 무형자산상각비" 합산 공시 → 분리 항목은 None
            final["무형자산상각비"] = None
            final["사용권자산상각비"] = None
    for k in _COST_KEYS:
        v = final.get(k)
        if v is not None and v < 0:
            final[k] = abs(v)
    return final


# ── 오프라인 실행 경계 ────────────────────────────────────────────────────────
def _patch_offline(stack: ExitStack) -> None:
    stack.enter_context(mock.patch("config.get_gemini_api_key", lambda: ""))
    stack.enter_context(mock.patch("config.get_dart_api_key", lambda: "TEST_FIXTURE_KEY"))


def _run_path_a(manifest: dict, raw_dir: Path, stack: ExitStack) -> dict:
    rows = json.loads((raw_dir / manifest["raw"]["financial_json"]).read_text("utf-8"))

    def _fake_fetch(corp_code, bsns_year, reprt_code, requested_div):
        # 캡처한 재무제표 기준만 반환하고 나머지는 "없음"으로 처리한다.
        if requested_div == manifest.get("requested_div", FS_DIV["consolidated"]):
            return rows
        raise ValueError(f"{requested_div} 미존재 (fixture)")

    stack.enter_context(
        mock.patch("financial.api_extractor.fetch_full_financial_statement", _fake_fetch)
    )
    from financial.api_extractor import get_financial_data_path_a

    return get_financial_data_path_a(
        manifest["corp_code"], manifest["year"], manifest["reprt_code"]
    )


def _run_path_b(manifest: dict, raw_dir: Path, stack: ExitStack) -> dict:
    xml = (raw_dir / manifest["raw"]["xml"]).read_bytes()
    stack.enter_context(
        mock.patch("financial.doc_extractor._download_dart_document", lambda rcept_no: xml)
    )
    from financial.doc_extractor import get_financial_data_path_b

    return get_financial_data_path_b(
        manifest["rcept_no"], report_type=manifest.get("report_type")
    )


def _run_depreciation(manifest: dict, raw_dir: Path, stack: ExitStack) -> dict:
    docs = [(raw_dir / manifest["raw"]["xml"]).read_bytes()]
    stack.enter_context(
        mock.patch(
            "depreciation.extractor._download_all_dart_documents", lambda rcept_no: docs
        )
    )
    from depreciation.extractor import extract_depreciation

    return extract_depreciation(
        manifest["rcept_no"],
        fs_div=manifest.get("fs_div", "CFS"),
        strict_scope=manifest.get("strict_scope", manifest["path"] == "A"),
    )


def build_result(manifest: dict, fixture_dir: str | Path) -> dict:
    """fixture를 오프라인으로 실행해 정규화된 결과 dict를 만든다(golden과 동일 구조)."""
    raw_dir = Path(fixture_dir)
    run = manifest.get("run", {})

    financials: dict[str, Any] = {n: None for n in _ITEM_NAMES}
    fs_div = manifest.get("fs_div")
    depr_norm: Optional[dict] = None

    with ExitStack() as stack:
        _patch_offline(stack)

        if run.get("financials", True):
            data = (
                _run_path_a(manifest, raw_dir, stack)
                if manifest["path"] == "A"
                else _run_path_b(manifest, raw_dir, stack)
            )
            items = data.get("items") or {}
            for n in _ITEM_NAMES:
                financials[n] = items.get(n)
            fs_div = data.get("fs_div")

        if run.get("depreciation", False):
            d = _run_depreciation(manifest, raw_dir, stack)
            depr_norm = {
                "items": {k: d.get("items", {}).get(k) for k in _DEPR_KEYS},
                "combined": bool(d.get("combined")),
                "source": d.get("source"),
            }

    final = merge_final_items(financials, depr_norm)
    return {
        "fs_div": fs_div,
        "financials": financials,
        "depreciation": depr_norm,
        "final_items": final,
        "ebitda": compute_ebitda(final),
    }


# ── golden 비교 ───────────────────────────────────────────────────────────────
def diff_result(actual: Any, golden: Any, *, path: str = "", abs_tol: float = 1.0,
                rel_tol: float = 1e-6) -> list[str]:
    """actual과 golden을 재귀 비교하고 불일치 경로 목록을 반환한다(빈 리스트=일치)."""
    diffs: list[str] = []

    if isinstance(golden, dict) and isinstance(actual, dict):
        for key in sorted(set(golden) | set(actual)):
            diffs += diff_result(
                actual.get(key, "<MISSING>"), golden.get(key, "<MISSING>"),
                path=f"{path}.{key}" if path else str(key),
                abs_tol=abs_tol, rel_tol=rel_tol,
            )
        return diffs

    if isinstance(golden, (int, float)) and isinstance(actual, (int, float)) \
            and not isinstance(golden, bool) and not isinstance(actual, bool):
        tol = max(abs_tol, abs(golden) * rel_tol)
        if abs(float(actual) - float(golden)) > tol:
            diffs.append(f"{path}: {actual} != {golden} (tol={tol})")
        return diffs

    if actual != golden:
        diffs.append(f"{path}: {actual!r} != {golden!r}")
    return diffs
