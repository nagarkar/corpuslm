"""Tests never touch a real database (metalm coding.md, "Sharing one
database between repos": the scrub removes each repo's pointer) and never
inherit a git hook's environment."""

import os

import pytest
from llmlm import prices

for key in [
    variable_name
    for variable_name in os.environ
    if variable_name.startswith("GIT_") or variable_name.endswith("_DB")
]:
    os.environ.pop(key, None)

# The rows corpuslm's tests price with, in litellm's map format (dollars per token). A test never
# reads the machine-wide price cache or the network.
_PRICE_MAP = {
    "gemini/gemini-2.5-flash-lite": (1e-7, 4e-7),
    "gemini/gemini-2.5-flash": (3e-7, 2.5e-6),
    "gemini/gemini-3.1-flash-lite": (2.5e-7, 1.5e-6),
    "anthropic/claude-opus-5-5": (4e-6, 2e-5),
    "anthropic/claude-sonnet-5-5": (2e-6, 1e-5),
    "anthropic/claude-haiku-5-5": (1e-7, 5e-7),
    "openai/gpt-6.1-sol": (2e-6, 1e-5),
}


@pytest.fixture(autouse=True)
def _fixed_price_cache(tmp_path, monkeypatch):
    path = tmp_path / "never-prices.json"
    table = {
        model: {"input_cost_per_token": low, "output_cost_per_token": high}
        for model, (low, high) in _PRICE_MAP.items()
    }
    monkeypatch.setattr(prices, "CACHE_PATH", path)
    monkeypatch.setattr(prices, "_default", prices.Prices(path, fetch=lambda: table))
