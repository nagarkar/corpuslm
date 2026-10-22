"""LLM: corpuslm's door to the shared model-call layer, `llmlm.llm`.

The transport, reply cache, ledger lines and spend caps live in llmlm; this module re-exports
them so corpuslm's modules keep their imports. `Client` is llmlm's class itself; `client()` builds
one with corpuslm's own reply validator and schema loader (`corpuslm.schemas`). The scripted
directory (`SCRIPTS`) and the price table are llmlm's: a test that scripts replies sets
`llmlm.llm.SCRIPTS`. Delete this shim once ytlm, beelm and mailerlm import llmlm directly
(nagarkar/llmlm#1, step 3).

Never imports: db, store, clikit.
"""

from __future__ import annotations

from llmlm import llm as _llmlm
from llmlm.llm import (
    ANY,
    CACHE_DIR,
    DAY_CAP,
    LOGS_DIR,
    MAX_ATTEMPTS,
    MODEL_PROFILES,
    REQUEST_TIMEOUT_S,
    RETRY_SLEEP_S,
    RETRY_STATUSES,
    RUN_CAP,
    UNKNOWN_PROFILE,
    VENDOR_DEFAULT_ONLY,
    BadReply,
    CacheMiss,
    Estimate,
    LiteLLMTransport,
    ModelProfile,
    NotConfigured,
    RawReply,
    Reply,
    ScriptedTransport,
    SpendCapReached,
    Transport,
    TruncatedReply,
    VendorError,
    check_temperatures,
    default_transport,
    model_profile,
    payload_sha256,
    price_usd,
    say_retry,
    unprofiled_models,
)

from . import schemas

# llmlm's own class, so that a function typed with `llm.Client` accepts the client a tool built
# from `llmlm.llm.Client` (ytlm, beelm) and the reverse. corpuslm's two construction sites pass its
# validator and schema loader (`corpuslm.schemas`) themselves; `client()` does the same for tests.
Client = _llmlm.Client


def client(*args, validator=None, schema_loader=None, **kwargs) -> Client:
    """llmlm's client with corpuslm's reply validator and schema loader as the defaults."""
    return Client(
        *args,
        validator=validator if validator is not None else schemas.validate,
        schema_loader=schema_loader if schema_loader is not None else schemas.schema,
        **kwargs,
    )


__all__ = [
    "ANY",
    "CACHE_DIR",
    "DAY_CAP",
    "LOGS_DIR",
    "MAX_ATTEMPTS",
    "MODEL_PROFILES",
    "REQUEST_TIMEOUT_S",
    "RETRY_SLEEP_S",
    "RETRY_STATUSES",
    "RUN_CAP",
    "UNKNOWN_PROFILE",
    "VENDOR_DEFAULT_ONLY",
    "BadReply",
    "CacheMiss",
    "Client",
    "Estimate",
    "LiteLLMTransport",
    "ModelProfile",
    "NotConfigured",
    "RawReply",
    "Reply",
    "ScriptedTransport",
    "SpendCapReached",
    "Transport",
    "TruncatedReply",
    "VendorError",
    "check_temperatures",
    "client",
    "default_transport",
    "model_profile",
    "payload_sha256",
    "price_usd",
    "say_retry",
    "unprofiled_models",
]
