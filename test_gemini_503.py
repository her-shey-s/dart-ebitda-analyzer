"""
Gemini API 503 진단 테스트
- gemini-3.1-flash-lite-preview 호출 → 503 여부 확인
- gemma-4-31b-it 호출 → fallback 가능 여부 확인
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    from google import genai
except ImportError:
    sys.exit("google-genai 패키지가 없습니다. pip install google-genai")

api_key = os.getenv("GEMINI_API_KEY", "")
if not api_key:
    if len(sys.argv) > 1:
        api_key = sys.argv[1]
    else:
        api_key = input("GEMINI_API_KEY를 입력하세요: ").strip()
if not api_key:
    sys.exit("API 키가 필요합니다.")

client = genai.Client(api_key=api_key)
prompt = "1 + 1 = ?"

models_to_test = [
    "gemini-3.1-flash-lite-preview",
    "gemma-4-31b-it",
]

for model in models_to_test:
    print(f"\n{'='*50}")
    print(f"테스트: {model}")
    print(f"{'='*50}")
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        print(f"  ✅ 성공: {response.text.strip()[:100]}")
    except Exception as e:
        err_str = str(e)
        if "503" in err_str or "UNAVAILABLE" in err_str:
            print(f"  ❌ 503 UNAVAILABLE (Google 측 문제 확인)")
        else:
            print(f"  ❌ 오류: {err_str[:200]}")

print("\n완료.")
