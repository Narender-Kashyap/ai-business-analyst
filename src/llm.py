"""Gemini client with retry + exponential backoff + fallback model."""
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv(override=True)      # .env wins over any stale Windows variable
# Main model first, then fallback. Can be overridden in .env: GEMINI_MODELS=model_a,model_b
MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3-flash-preview,gemini-flash-lite-latest").split(",")
          if m.strip()] or ["gemini-3-flash-preview", "gemini-flash-lite-latest"]
RETRYABLE_CODES = {429, 500, 502, 503, 504}                     # rate limit / server busy
MAX_ATTEMPTS_PER_MODEL = 3

_client = None


def _reason(e):
    """Turn an API error into a short reason a person can act on."""
    text = f"{e.message} {getattr(e, 'details', '')}".lower()
    if e.code == 429 and ("per day" in text or "perday" in text or "free_tier" in text):
        return "daily free-tier quota used up (resets at midnight Pacific time)"
    if e.code == 429:
        return "rate limited (too many requests per minute); try again shortly"
    if e.code in (500, 502, 503, 504):
        return "overloaded right now; try again in a minute"
    if e.code == 404:
        return "model not available for this API key"
    if e.code in (400, 401, 403):
        return "API key rejected; check GEMINI_API_KEY in .env"
    return f"HTTP {e.code} {e.status}"


class LLMUnavailable(RuntimeError):
    """Every model failed. str() is a short summary; .details has the raw errors for the trace."""
    def __init__(self, summary, details):
        super().__init__(summary)
        self.details = details


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
    failures, reasons = [], {}
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
                reasons[model] = _reason(e)
                if reasons[model].startswith("daily"):        # waiting won't help: go to the next model
                    break
                if e.code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS_PER_MODEL:
                    time.sleep(2 ** attempt)                  # 2s, 4s: exponential backoff
                    continue
                break                                         # not retryable -> next model
            except Exception as e:                            # network error, missing key etc.
                failures.append(f"{model} attempt {attempt}: {type(e).__name__}: {e}")
                reasons[model] = f"{type(e).__name__}: {e}"
                if "GEMINI_API_KEY is not set" in str(e):
                    raise
                break
    summary = "; ".join(f"{m}: {r}" for m, r in reasons.items())
    raise LLMUnavailable(f"The AI model is not available right now ({summary}). The validator tab "
                         f"still works without it.", failures)
