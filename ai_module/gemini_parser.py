"""
ai_module/gemini_parser.py
Gemini를 이용한 재무 데이터 AI 추출·검증

역할 (경로A·B 공통):
  1. ai_extract_items()           : 재무제표 원문에서 항목 독립 추출 (AI 1회차)
  2. ai_adjudicate()              : Python vs AI 불일치 시 원문 기반 판정 (AI 2회차)
  3. extract_with_ai_comparison() : 위 두 함수를 조율하는 오케스트레이터
  4. extract_from_raw_text()      : 파싱 실패 시 테이블 텍스트에서 AI로 재추출
"""

import json
import re
import time
from typing import Optional

from config import FINANCIAL_ITEMS, get_gemini_api_key, GEMINI_MODEL, GEMINI_FALLBACK_MODEL

# ── Rate limit 설정 ──────────────────────────────────────────────────────
_RATE_LIMIT_RPM = 15          # 분당 최대 호출 횟수
_RATE_LIMIT_WAIT = 30         # 한도 도달 시 대기 시간(초)
_call_count = 0               # 현재 윈도우 내 호출 횟수
_window_start = 0.0           # 현재 윈도우 시작 시각


# ── 내부 유틸 ──────────────────────────────────────────────────────────────

def _get_client():
    """
    google-genai 클라이언트를 반환한다. API 키 없으면 None.

    Returns:
        google.genai.Client 또는 None
    """
    if not get_gemini_api_key():
        return None
    try:
        from google import genai
        return genai.Client(api_key=get_gemini_api_key())
    except Exception:
        return None


def _wait_if_rate_limited(log_fn=None) -> None:
    """분당 _RATE_LIMIT_RPM회에 도달하면 _RATE_LIMIT_WAIT초 대기한다."""
    global _call_count, _window_start

    now = time.time()
    # 60초 경과 시 윈도우 리셋
    if now - _window_start >= 60:
        _call_count = 0
        _window_start = now

    if _call_count >= _RATE_LIMIT_RPM:
        if log_fn:
            log_fn("AI", f"    Rate limit 도달 ({_RATE_LIMIT_RPM}RPM) → {_RATE_LIMIT_WAIT}초 대기...")
        time.sleep(_RATE_LIMIT_WAIT)
        _call_count = 0
        _window_start = time.time()

    _call_count += 1


def _generate(client, prompt: str, log_fn=None) -> Optional[str]:
    """
    Gemini로 텍스트를 생성하고 응답 문자열을 반환한다.

    분당 _RATE_LIMIT_RPM회 호출 후 _RATE_LIMIT_WAIT초 대기한다.
    503 UNAVAILABLE 시 GEMINI_FALLBACK_MODEL로 자동 재시도.

    Args:
        client: _get_client() 반환값
        prompt: 프롬프트 문자열
        log_fn: 로그 콜백 (tag, message) → None (선택)

    Returns:
        응답 텍스트 또는 None (오류 시)
    """
    _log = log_fn or (lambda tag, msg: None)
    _wait_if_rate_limited()
    for model in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            _log("AI", f"    모델 사용: {model}")
            return response.text.strip()
        except Exception as e:
            err_str = str(e)
            if ("503" in err_str or "UNAVAILABLE" in err_str) and model != GEMINI_FALLBACK_MODEL:
                _log("AI", f"    {model} → 503 UNAVAILABLE, fallback → {GEMINI_FALLBACK_MODEL}")
                continue
            raise RuntimeError(f"Gemini API 호출 실패 ({model}): {e}") from e


def _parse_json(text: str) -> Optional[dict]:
    """
    응답 텍스트에서 JSON 객체를 추출한다.

    ```json ... ``` 블록과 plain JSON 모두 처리한다.

    Args:
        text: Gemini 응답 문자열

    Returns:
        dict 또는 None (파싱 실패 시)
    """
    # ```json ... ``` 또는 ``` ... ``` 블록 추출
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


# ── 경로B: AI 추출 + 비교 검증 ─────────────────────────────────────────────
#
# Python 파싱 결과와 AI 독립 추출 결과를 비교하여 최종 값을 결정한다.
# AI 호출: 최소 1회(추출), 불일치 시 최대 2회(추출 + 판정).
#

_ITEM_NAMES = [item["name"] for item in FINANCIAL_ITEMS]


def _build_extraction_prompt(table_text: str) -> str:
    """AI 추출용 프롬프트를 생성한다."""
    fs_type_labels = {"BS": "재무상태표", "IS": "손익계산서", "CF": "현금흐름표"}
    item_lines = []
    for item in FINANCIAL_ITEMS:
        kws = ", ".join(item["keywords"])
        fs_label = fs_type_labels.get(item["fs_type"], "")
        item_lines.append(f'  - {item["name"]} [{fs_label}] (표기: {kws})')
    item_list = "\n".join(item_lines)

    return (
        "아래는 한국 기업의 감사보고서에서 추출한 재무상태표, 손익계산서, 현금흐름표 원문이다.\n"
        "다음 항목들의 '당기' 금액을 원(KRW) 단위 숫자로 추출해라.\n"
        "괄호 표기 (예: (1,234))는 음수를 의미한다.\n"
        "감가상각비·무형자산상각비는 [CF](현금흐름표)의 영업활동 조정항목에서 찾아라.\n"
        "찾을 수 없는 항목은 null로 표시해라.\n"
        "JSON으로만 응답해라. 다른 텍스트 없이.\n\n"
        f"추출 항목:\n{item_list}\n\n"
        f"응답 형식:\n"
        f'{{"총자산": 1234567890, "총부채": null, ...}}\n\n'
        f"재무제표 원문:\n---\n{table_text}\n---"
    )


def _build_adjudication_prompt(
    table_text: str,
    disagreements: dict[str, tuple],
) -> str:
    """AI 판정용 프롬프트를 생성한다. 편향 방지를 위해 A/B로 익명 표기."""
    diff_lines = []
    for name, (val_a, val_b) in disagreements.items():
        a_str = f"{val_a:,.0f}" if val_a is not None else "없음"
        b_str = f"{val_b:,.0f}" if val_b is not None else "없음"
        diff_lines.append(f"  - {name}: 추출A={a_str}, 추출B={b_str}")
    diff_text = "\n".join(diff_lines)

    return (
        "아래 재무제표 원문을 두 가지 방법으로 추출했더니 결과가 다르다.\n"
        "원문을 직접 확인하여 각 항목의 올바른 '당기' 금액을 판정해라.\n"
        "금액은 원(KRW) 단위 숫자, 괄호는 음수, 찾을 수 없으면 null.\n"
        "JSON으로만 응답해라.\n\n"
        f"불일치 항목:\n{diff_text}\n\n"
        f"응답 형식:\n"
        f'{{"항목명": 올바른금액또는null}}\n\n'
        f"재무제표 원문:\n---\n{table_text}\n---"
    )


def ai_extract_items(table_text: str, log_fn=None) -> dict[str, Optional[float]]:
    """
    재무제표 원문 테이블 텍스트에서 AI로 9개 항목을 독립 추출한다.

    경로B의 AI 1회차 호출. Python 파싱 결과와 무관하게 독립적으로 추출한다.

    Args:
        table_text: _extract_table_text_for_ai() 반환값
        log_fn: 로그 콜백 (tag, message) → None (선택)

    Returns:
        {항목명: 금액(float) | None, ...}

    Raises:
        RuntimeError: Gemini API 호출 또는 응답 파싱 실패
    """
    client = _get_client()
    if client is None:
        raise RuntimeError("Gemini API 키가 설정되지 않았습니다.")

    prompt = _build_extraction_prompt(table_text)
    raw = _generate(client, prompt, log_fn=log_fn)
    parsed = _parse_json(raw)
    if parsed is None:
        raise RuntimeError(f"AI 응답 JSON 파싱 실패: {raw[:300]}")

    result: dict[str, Optional[float]] = {}
    for name in _ITEM_NAMES:
        val = parsed.get(name)
        try:
            result[name] = float(val) if val is not None else None
        except (TypeError, ValueError):
            result[name] = None

    return result


def _compare_results(
    python_items: dict[str, Optional[float]],
    ai_items: dict[str, Optional[float]],
    rel_tol: float = 0.001,
    abs_tol: float = 1.0,
) -> dict[str, tuple]:
    """
    Python 추출 결과와 AI 추출 결과를 비교하여 불일치 항목을 반환한다.

    비교 기준:
      - 둘 다 None → 일치
      - 하나만 None → 불일치
      - 둘 다 숫자 → 상대오차 > rel_tol AND 절대오차 > abs_tol → 불일치

    Args:
        python_items: Python 파싱 결과
        ai_items:     AI 추출 결과
        rel_tol:      상대 허용 오차 (0.1%)
        abs_tol:      절대 허용 오차 (1원)

    Returns:
        {항목명: (python값, ai값)} — 불일치 항목만
    """
    disagreements: dict[str, tuple] = {}

    for name in _ITEM_NAMES:
        py_val = python_items.get(name)
        ai_val = ai_items.get(name)

        # 둘 다 None → 일치
        if py_val is None and ai_val is None:
            continue

        # 하나만 None → 불일치
        if py_val is None or ai_val is None:
            disagreements[name] = (py_val, ai_val)
            continue

        # 둘 다 숫자 → 오차 비교
        diff = abs(py_val - ai_val)
        base = max(abs(py_val), abs(ai_val), 1.0)
        if diff > abs_tol and diff / base > rel_tol:
            disagreements[name] = (py_val, ai_val)

    return disagreements


def ai_adjudicate(
    table_text: str,
    python_items: dict[str, Optional[float]],
    ai_items: dict[str, Optional[float]],
    disagreements: dict[str, tuple],
    log_fn=None,
) -> dict[str, Optional[float]]:
    """
    Python vs AI 불일치 항목에 대해 AI가 원문을 보고 올바른 값을 판정한다.

    경로B의 AI 2회차 호출. 편향 방지를 위해 '추출A/추출B'로 익명 표기한다.

    Args:
        table_text:    재무제표 원문
        python_items:  Python 파싱 결과
        ai_items:      AI 1회차 추출 결과
        disagreements: _compare_results() 반환값
        log_fn: 로그 콜백 (tag, message) → None (선택)

    Returns:
        최종 병합 결과 {항목명: 금액(float) | None}
        (일치 항목은 python_items 값 유지, 불일치 항목은 판정 결과로 교체)

    Raises:
        RuntimeError: Gemini API 호출 또는 응답 파싱 실패
    """
    client = _get_client()
    if client is None:
        raise RuntimeError("Gemini API 키가 설정되지 않았습니다.")

    # 편향 방지: A/B를 랜덤하게 배정하지 않고 고정 (Python=A, AI=B)
    # → 프롬프트에는 어느 쪽이 Python/AI인지 명시하지 않음
    prompt = _build_adjudication_prompt(table_text, disagreements)
    raw = _generate(client, prompt, log_fn=log_fn)
    parsed = _parse_json(raw)
    if parsed is None:
        raise RuntimeError(f"AI 판정 응답 JSON 파싱 실패: {raw[:300]}")

    # 최종 결과: python_items 기반으로 판정 결과 병합
    final = dict(python_items)
    for name in disagreements:
        val = parsed.get(name)
        try:
            final[name] = float(val) if val is not None else None
        except (TypeError, ValueError):
            # 판정 실패 시 AI 1회차 결과 사용 (Python보다 원문 기반)
            final[name] = ai_items.get(name)

    return final


def extract_with_ai_comparison(
    table_text: str,
    python_items: dict[str, Optional[float]],
    log_fn=None,
) -> dict:
    """
    경로B 오케스트레이터: AI 추출 + Python 비교 + 불일치 시 AI 판정.

    흐름:
      1. AI로 9개 항목 독립 추출 (1회차 호출)
      2. Python 결과와 비교
      3. 일치 → Python 결과 반환
      4. 불일치 → AI에게 원문 + 양쪽 결과를 주고 판정 요청 (2회차 호출)

    AI 호출 실패 시 Python 결과로 graceful fallback.

    Args:
        table_text:   _extract_table_text_for_ai() 반환값
        python_items: _extract_all_items() 반환값
        log_fn:       로그 콜백 (tag, message) → None (선택)

    Returns:
        {
            "items":         최종 항목 딕셔너리,
            "source":        "agreed" | "adjudicated" | "python_fallback",
            "ai_calls":      AI 호출 횟수 (0, 1, 2),
            "disagreements": {항목명: (python값, ai값)},  # 불일치 내역
            "ai_items":      AI 1회차 추출 결과 (디버깅용),
            "error":         오류 메시지 또는 None,
        }
    """
    _log = log_fn or (lambda tag, msg: None)

    base = {
        "items":         python_items,
        "source":        "python_fallback",
        "ai_calls":      0,
        "disagreements": {},
        "ai_items":      None,
        "error":         None,
    }

    # 1. AI 독립 추출 (1회차)
    _log("AI", "    AI 1회차: 독립 추출 호출...")
    t0 = time.perf_counter()
    try:
        ai_items = ai_extract_items(table_text, log_fn=log_fn)
    except RuntimeError as e:
        elapsed = time.perf_counter() - t0
        _log("AI", f"    AI 1회차 실패 ({elapsed:.2f}초): {e}")
        return {**base, "error": f"AI 추출 실패: {e}"}
    elapsed = time.perf_counter() - t0
    _log("AI", f"    AI 1회차 완료 ({elapsed:.2f}초)")

    base["ai_calls"] = 1
    base["ai_items"] = ai_items

    # 2. 비교
    disagreements = _compare_results(python_items, ai_items)
    base["disagreements"] = disagreements

    if not disagreements:
        # 완전 일치 → Python 결과 사용 (AI가 확인해준 셈)
        _log("AI", "    Python-AI 완전 일치 (agreed)")
        return {**base, "source": "agreed"}

    _log("AI", f"    불일치 {len(disagreements)}건: {', '.join(disagreements.keys())}")

    # 3. 불일치 → AI 판정 (2회차)
    _log("AI", "    AI 2회차: 판정 호출...")
    t0 = time.perf_counter()
    try:
        final_items = ai_adjudicate(table_text, python_items, ai_items, disagreements, log_fn=log_fn)
    except RuntimeError as e:
        elapsed = time.perf_counter() - t0
        _log("AI", f"    AI 2회차 실패 ({elapsed:.2f}초): {e}")
        # 2회차 실패 → AI 1회차 결과로 fallback (원문을 본 결과이므로)
        merged = dict(python_items)
        for name, val in ai_items.items():
            if val is not None and merged.get(name) is None:
                merged[name] = val
        return {
            **base,
            "items":  merged,
            "source": "ai_extract_only",
            "error":  f"AI 판정 실패, 1회차 결과 사용: {e}",
        }
    elapsed = time.perf_counter() - t0
    _log("AI", f"    AI 2회차 완료 ({elapsed:.2f}초, adjudicated)")

    base["ai_calls"] = 2
    return {**base, "items": final_items, "source": "adjudicated"}


def extract_from_raw_text(
    table_text: str,
    missing_items: list[str],
    log_fn=None,
) -> dict[str, Optional[float]]:
    """
    재무제표 테이블 텍스트에서 Gemini Flash로 항목을 재추출한다.

    html_parser가 4개 이상 항목을 찾지 못했을 때 호출한다.
    텍스트는 3000자로 잘라 토큰을 절약한다.

    Args:
        table_text:    재무제표 테이블 원문 텍스트
        missing_items: 찾지 못한 항목명 리스트

    Returns:
        {항목명: 금액(float) | None, ...}
        API 실패 또는 키 미설정 시 빈 딕셔너리
    """
    client = _get_client()
    if client is None or not missing_items:
        return {}

    items_str = ", ".join(missing_items)
    truncated = table_text[:3000]

    prompt = (
        f"아래 재무제표 텍스트에서 다음 항목들의 금액을 찾아줘: {items_str}\n"
        f"단위를 정확히 확인하고 원(KRW) 단위 숫자로 변환해서 JSON으로만 응답해:\n"
        f'{{"항목명":금액또는null}}\n'
        f"텍스트:\n{truncated}"
    )

    try:
        raw = _generate(client, prompt, log_fn=log_fn)
    except RuntimeError:
        return {}

    parsed = _parse_json(raw)
    if parsed is None:
        return {}

    result: dict[str, Optional[float]] = {}
    for k, v in parsed.items():
        if k in missing_items:
            try:
                result[k] = float(v) if v is not None else None
            except (TypeError, ValueError):
                result[k] = None

    return result
