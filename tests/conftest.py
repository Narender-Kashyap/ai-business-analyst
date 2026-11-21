"""Shared test setup.

Runs every test from the project root (the code reads config/ and data/ with relative paths),
puts src/ on the import path, and provides a fake Gemini so no test needs an API key.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

LAST_WEEK = {"previous": ("2026-11-02", "2026-11-08"), "current": ("2026-11-09", "2026-11-15")}
QUIET_WEEK = {"previous": ("2026-10-19", "2026-10-25"), "current": ("2026-10-26", "2026-11-01")}


def fake_llm(response):
    """A stand-in for llm.generate_json that always returns `response` as JSON."""
    def generate_json(prompt):
        return {"text": json.dumps(response), "model": "fake", "attempts": 1, "failures": [],
                "latency_s": 0.0, "input_tokens": 0, "output_tokens": 0}
    return generate_json


@pytest.fixture
def use_fake_llm(monkeypatch):
    """Usage: use_fake_llm(plan=..., narrative=...) replaces both Gemini calls for one test."""
    import answer
    import planner

    def install(plan, narrative=None):
        monkeypatch.setattr(planner, "generate_json", fake_llm(plan))
        monkeypatch.setattr(answer, "generate_json",
                            fake_llm(narrative or {"headline": "ok", "hypotheses": [], "recommendations": []}))
    return install


def plan(question_type, queries=None, **extra):
    """Build a plan the way Gemini would return it."""
    p = {"question_type": question_type, "interpretation": "test", "metric": None,
         "time_window": None, "filters": [], "assumptions": [], "message": None, "queries": queries or []}
    p.update(extra)
    return p


@pytest.fixture(autouse=True)
def separate_trace_file(tmp_path, monkeypatch):
    """Tests write their trace lines to a temporary file, not the real traces/trace.jsonl."""
    import pipeline
    monkeypatch.setattr(pipeline, "TRACE_FILE", tmp_path / "trace.jsonl")
