"""Ledger: corpuslm's door to `llmlm.ledger`, the one usage and trace ledger writer and reader.

A re-export so corpuslm's modules keep their imports. Delete this shim once ytlm, beelm and
mailerlm import llmlm directly (nagarkar/llmlm#1, step 3).

Never imports: db, store, llm, clikit.
"""

from __future__ import annotations

from llmlm.ledger import DEFAULTS, GENERATIONS, ROTATE_BYTES, Ledger, read

__all__ = ["DEFAULTS", "GENERATIONS", "ROTATE_BYTES", "Ledger", "read"]
