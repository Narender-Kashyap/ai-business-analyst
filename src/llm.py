"""Gemini client with retry + exponential backoff + fallback model."""
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv(override=True)      # .env wins over any stale Windows variable
# Main model first, then fallback. Can be overridden in .env: GEMINI_MODELS=model_a,model_b
MODELS = os.getenv("GEMINI_MODELS", "gemini-3-flash-preview,gemini-flash-lite-latest").split(",")
RETRYABLE_CODES = {429, 500, 502, 503, 504}                     # rate limit / server busy
MAX_ATTEMPTS_PER_MODEL = 3

_client = None


def get_client():
    global _client
    if _client is None:
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and put your Gemini "
                               "API key in it (https://aistudio.google.com/apikey).")
        _client = genai.Client(
            api_key=os.getenv("GEMINI_API_KEY"),
            http_options=types.HttpOptions(api_version="v1beta"),   # preview models live in v1beta
        )
    return _client


def generate_json(prompt):
    """Ask Gemini for a JSON answer. Returns a dict with the text and trace info."""
    failures = []
    for model in MODELS:
        for attempt in range(1, MAX_ATTEMPTS_PER_MODEL + 1):
            start = time.time()
            try:
                response = get_client().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0,                        # as deterministic as possible
                        response_mime_type="application/json",
                    ),
                )
                if not response.text:                        # empty / blocked response -> try again
                    raise ValueError("empty response")
                usage = response.usage_metadata              # may be missing: never lose an answer over it
                return {
                    "text": response.text,
                    "model": model,
                    "attempts": attempt,
                    "failures": failures,
                    "latency_s": round(time.time() - start, 2),
                    "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
                    "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
                }
            except errors.APIError as e:
                failures.append(f"{model} attempt {attempt}: HTTP {e.code} {e.status} - {e.message}")
                if e.code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS_PER_MODEL:
                    time.sleep(2 ** attempt)                  # 2s, 4s: exponential backoff
                    continue
                break                                         # not retryable -> next model
            except Exception as e:                            # network error, missing key etc.
                failures.append(f"{model} attempt {attempt}: {type(e).__name__}: {e}")
                if "GEMINI_API_KEY is not set" in str(e):
                    raise
                break
    raise RuntimeError("All models failed: " + "; ".join(failures))
