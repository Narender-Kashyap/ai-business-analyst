"""Diagnose the .env key without revealing it.

Run:  python src/check_env.py
"""
import os

from dotenv import load_dotenv

load_dotenv()
key = os.getenv("GEMINI_API_KEY")

if key is None:
    print("GEMINI_API_KEY not found. Is .env in the project root, and is the name spelled exactly?")
else:
    print("length          :", len(key))
    print("starts with     :", key[:4] + "...")
    print("has quotes      :", '"' in key or "'" in key)
    print("has spaces      :", " " in key)
    print("has extra lines :", "\n" in key or "\r" in key)

with open(".env", "rb") as f:
    raw = f.read()
print("lines in .env   :", raw.count(b"\n") + 1)
print("starts with BOM :", raw.startswith(b"\xef\xbb\xbf"))

load_dotenv(override=True)
print("key in use ends :", "..." + os.getenv("GEMINI_API_KEY")[-4:])