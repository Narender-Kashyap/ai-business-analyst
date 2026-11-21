"""List the Gemini 'flash' models this API key can call.

Run:  python src/list_models.py
"""
from llm import get_client

for model in get_client().models.list():
    if "gemini" in model.name and "flash" in model.name:
        print(model.name)
