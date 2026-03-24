"""
ai_module/gemini_parser.py
Gemini Flash API 연동

두 가지 용도로 사용:
  1. 파싱 폴백: HTML 파싱에 실패한 경우 Gemini에게 재무 항목 추출 요청
  2. AI 검증:   규칙/교차 검증에서 플래그된 항목을 Gemini에게 재검증 요청

비용 최소화를 위해 HTML 전문 대신 요약된 테이블 텍스트만 전달한다.
"""

import json
from typing import Optional

import google.generativeai as genai

from config import FINANCIAL_ITEMS, GEMINI_API_KEY, GEMINI_MODEL


def _get_model():
    """Gemini 모델 인스턴스를 초기화하여 반환한다."""
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel(GEMINI_MODEL)


def _build_item_list_text() -> str:
    """프롬프트에 삽입할 추출 항목 목록 텍스트를 생성한다."""
    lines = []
    for item in FINANCIAL_ITEMS:
        kws = ", ".join(item["keywords"])
        lines.append(f'- {item["name"]} (키워드: {kws})')
    return "\n".join(lines)


def extract_financials_with_gemini(
    table_text: str,
    corp_name: str,
    year: int,
) -> dict[str, Optional[float]]:
    """
    Gemini Flash를 사용하여 재무제표 텍스트에서 항목별 금액을 추출한다.

    HTML 파싱 실패 시 폴백으로 호출된다.
    테이블 텍스트를 요약하여 전달하므로 토큰을 최소화한다.

    Args:
        table_text: 재무제표가 포함된 텍스트 (HTML이 아닌 순수 텍스트)
        corp_name:  기업명 (컨텍스트 제공용)
        year:       사업연도

    Returns:
        항목명 → 금액(float) 딕셔너리. 찾지 못하면 None.

    Raises:
        RuntimeError: Gemini API 호출 실패 또는 JSON 파싱 실패 시
    """
    item_list = _build_item_list_text()
    item_names = [item["name"] for item in FINANCIAL_ITEMS]

    prompt = f"""
다음은 {corp_name}의 {year}년 재무제표 텍스트입니다.
아래 항목들의 금액을 추출하여 JSON 형식으로만 응답하세요.
금액 단위는 원(KRW)이며, 숫자만 반환하세요 (쉼표 없이).
찾을 수 없는 항목은 null로 표시하세요.

추출 항목:
{item_list}

응답 형식 (JSON만, 다른 텍스트 없이):
{{
  "총자산": 1234567890,
  "총부채": null,
  ...
}}

재무제표 텍스트:
---
{table_text[:8000]}
---
"""

    model = _get_model()
    response = model.generate_content(prompt)
    raw_text = response.text.strip()

    # 코드 블록 제거
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini 응답 JSON 파싱 실패: {e}\n응답: {raw_text[:300]}")

    result: dict[str, Optional[float]] = {}
    for name in item_names:
        val = parsed.get(name)
        result[name] = float(val) if val is not None else None

    return result


def validate_with_gemini(
    items: dict[str, Optional[float]],
    flags: list[str],
    corp_name: str,
    year: int,
) -> dict:
    """
    규칙/교차 검증에서 이상 플래그가 달린 항목을 Gemini로 재검증한다.

    Args:
        items:     항목명 → 금액 딕셔너리
        flags:     이상이 감지된 항목명 리스트 또는 설명 리스트
        corp_name: 기업명
        year:      사업연도

    Returns:
        {
            "is_valid":  bool,
            "issues":    [이슈 설명 문자열 리스트],
            "corrected": {항목명: 수정 금액} (수정 제안이 있는 경우),
        }

    Raises:
        RuntimeError: API 호출 또는 응답 파싱 실패 시
    """
    items_text = "\n".join(f"  {k}: {v:,.0f}" if v else f"  {k}: 없음" for k, v in items.items())
    flags_text = "\n".join(f"  - {f}" for f in flags)

    prompt = f"""
다음은 {corp_name}의 {year}년 재무 데이터입니다.
자동 검증에서 아래 이상 항목들이 감지되었습니다.
회계사 관점에서 이상 여부를 판단하고 JSON으로만 응답하세요.

재무 데이터:
{items_text}

감지된 이상:
{flags_text}

응답 형식 (JSON만):
{{
  "is_valid": true,
  "issues": ["설명1", "설명2"],
  "corrected": {{}}
}}
"""

    model = _get_model()
    response = model.generate_content(prompt)
    raw_text = response.text.strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini 검증 응답 파싱 실패: {e}")
