"""Smoke test: can we reach Gemini, and does the fallback/trace info come back?

Run:  python src/test_llm.py
"""
from llm import generate_json

result = generate_json('Return this JSON exactly: {"ok": true}')
for key, value in result.items():
    print(f"{key:15s}: {value}")