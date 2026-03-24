"""
gemini_parser.py
Gemini Flash를 이용한 재무 데이터 AI 검증 및 재추출

두 가지 역할:
  1. verify_financial_data()  : critical 실패 항목을 AI로 재검토
  2. extract_from_raw_text()  : 파싱 실패 시 테이블 텍스트에서 AI로 재추출
"""

import json
import re
from typing import Optional

from config import GEMINI_API_KEY, GEMINI_MODEL


# ── 내부 유틸 ──────────────────────────────────────────────────────────────

def _get_client():
    """
    google-genai 클라이언트를 반환한다. API 키 없으면 None.

    Returns:
        google.genai.Client 또는 None
    """
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        return None


def _generate(client, prompt: str) -> Optional[str]:
    """
    Gemini Flash로 텍스트를 생성하고 응답 문자열을 반환한다.

    Args:
        client: _get_client() 반환값
        prompt: 프롬프트 문자열

    Returns:
        응답 텍스트 또는 None (오류 시)
    """
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return response.text.strip()
    except Exception:
        return None


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


# ── 공개 API ───────────────────────────────────────────────────────────────

def verify_financial_data(
    items: dict[str, Optional[float]],
    validation: dict,
    corp_name: str,
    year: int,
) -> dict:
    """
    Gemini Flash로 재무 데이터 검증 결과를 AI 검토한다.

    실패한 checks 정보와 관련 항목 수치만 전송하여 토큰을 최소화한다.
    API 호출 실패 또는 키 미설정 시 graceful하게 처리한다.

    Args:
        items:      항목명 → 금액(원) 딕셔너리
        validation: validate() 반환값
        corp_name:  기업명 (컨텍스트용)
        year:       사업연도

    Returns:
        {
            "verdict":     "correct" | "error" | "uncertain" | "skipped",
            "issues":      [문제점 설명, ...],
            "corrections": {항목명: 수정값(float), ...},
            "raw_response": str (원본 응답, 디버깅용, 오류 시 생략),
        }
    """
    client = _get_client()
    if client is None:
        return {"verdict": "skipped", "issues": ["GEMINI_API_KEY 미설정"], "corrections": {}}

    failed_checks = [c for c in validation.get("checks", []) if not c["passed"]]
    if not failed_checks:
        return {"verdict": "correct", "issues": [], "corrections": {}}

    # 실패 규칙에 언급된 항목만 추출 (전체 전송 금지)
    relevant: dict[str, float] = {}
    for name, val in items.items():
        if val is not None and any(name in c.get("rule", "") for c in failed_checks):
            relevant[name] = val
    if not relevant:
        relevant = {k: v for k, v in items.items() if v is not None}

    items_str  = ", ".join(f"{k}={v:,.0f}원" for k, v in relevant.items())
    checks_str = "; ".join(
        f"{c['rule']}(차이={c['diff']:,.0f}원)" if isinstance(c.get("diff"), (int, float))
        else c["rule"]
        for c in failed_checks
    )

    prompt = (
        f"숙련된 재무분석가로서 {corp_name} {year}년 재무데이터를 검토해줘.\n"
        f"데이터(원KRW): {items_str}\n"
        f"검증실패: {checks_str}\n"
        f"데이터가 맞는지 틀린지 판단하고 JSON으로만 응답해:\n"
        f'{{"verdict":"correct"또는"error"또는"uncertain",'
        f'"issues":["문제설명"],'
        f'"corrections":{{"항목명":수정값}}}}'
    )

    raw = _generate(client, prompt)
    if raw is None:
        return {"verdict": "skipped", "issues": ["AI 호출 실패"], "corrections": {}}

    parsed = _parse_json(raw)
    if parsed is None:
        return {
            "verdict":      "uncertain",
            "issues":       ["AI 응답 JSON 파싱 실패"],
            "corrections":  {},
            "raw_response": raw,
        }

    # 숫자형 corrections 정규화
    corrections: dict[str, float] = {}
    for k, v in parsed.get("corrections", {}).items():
        try:
            corrections[k] = float(v)
        except (TypeError, ValueError):
            pass

    return {
        "verdict":      parsed.get("verdict", "uncertain"),
        "issues":       parsed.get("issues", []),
        "corrections":  corrections,
        "raw_response": raw,
    }


def extract_from_raw_text(
    table_text: str,
    missing_items: list[str],
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

    raw = _generate(client, prompt)
    if raw is None:
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
