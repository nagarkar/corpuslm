"""Prompts: corpuslm's door to `llmlm.prompts`, with corpuslm's own built-in directory.

The loader lives in llmlm. corpuslm's own prompt files (`src/corpuslm/prompts/`) are registered
there when this module is imported, first in the search order, so `load` finds them as it always
has. Delete the re-export once ytlm, beelm and mailerlm import llmlm directly (nagarkar/llmlm#1,
step 3); the registration stays, called from corpuslm's own modules.

Never imports: db, store, llm, clikit.
"""

from __future__ import annotations

from pathlib import Path

from llmlm.prompts import (
    Prompt,
    PromptError,
    list_prompts,
    load,
    register_directory,
    search_directories,
    versions,
)

BUILT_IN_DIRECTORY = Path(__file__).parent / "prompts"

register_directory(BUILT_IN_DIRECTORY)

__all__ = [
    "BUILT_IN_DIRECTORY",
    "Prompt",
    "PromptError",
    "list_prompts",
    "load",
    "register_directory",
    "search_directories",
    "versions",
]
