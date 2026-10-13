"""Review settings: the one small file the review server and its verbs read.

The shared transcript database holds every kind's sources, but what each kind keeps beside it is
its own: the folder holding its per-source documents, and, for Bee, the owner's first name. The
settings file (`review.toml`, kept beside the database, never in git) names them, so one server can
serve every kind without importing ytlm or beelm. See `scripts/review.example.toml`.

    database = "/path/to/transcript.sqlite"
    port = 8781

    [kinds.youtube]
    directory = "/path/to/ytlm/videos"

    [kinds.bee]
    directory = "~/Bee/beelm/transcripts"
    owner_first_name = "Quill"

`directory` holds `<source id>/transcript.json` and `<source id>/speaker_map.json` for each source
of the kind. An optional `[claims]` table turns on the claim pass (`corpuslm claims build`): the
model, the section size, and the folder where the model client keeps its reply cache and
ledgers; an optional `[llm]` table holds the spend caps the client enforces:

    [claims]
    model = "gemini/gemini-2.5-flash-lite"
    section_chars = 6000
    workspace = "/path/to/corpuslm"

    [llm]
    max_usd_per_run = 1.0
    max_usd_per_day = 5.0

An optional `[concepts]` table turns on the concept pass (`corpuslm concepts build`), which shares
the `[claims]` workspace: the model, and the gate (`min_sections`, `min_claims`) and batch size:

    [concepts]
    model = "gemini/gemini-2.5-flash-lite"
    min_sections = 3
    min_claims = 5
    batch_claims = 120

An optional `[moments]` table turns on the moment proposal pass (`corpuslm moments propose`),
sharing the `[claims]` workspace: the model and how many moments a source may get at most:

    [moments]
    model = "gemini/gemini-2.5-flash-lite"
    max_moments = 8

An optional `[usage]` table tells the usage dashboard (`corpuslm.usage`) where each
tool keeps its ledger folder, and where its own settings file holds the spend caps:

    [usage]
    port = 8779

    [usage.ledgers]
    ytlm = "/path/to/ytlm/workspace/logs"
    beelm = "~/Bee/beelm/logs"

    [usage.tool_settings]
    ytlm = "/path/to/ytlm/config.toml"
    beelm = "/path/to/beelm/config.toml"

The file is read strictly: an unknown section or key, a wrong type or a missing
`database` is named and declines.

Never imports: cli, clikit, db, any model transport.

Decisions:
- The database path lives in this file and nowhere on a command line, so a process listing never
  carries it. Enforced by:
  tests/test_review_settings.py::test_the_database_path_comes_from_the_file.
- The file is strict: an unknown key or section, a wrong type, a source kind there is no review
  kind for, and a kind with no folder are each named. Enforced by:
  tests/test_review_settings.py::test_an_unknown_key_is_named_and_declined.

Terms:
- **review settings**: this file, parsed.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .review_kinds import KINDS, kinds_for_settings
from .review_page import KindSetup

DEFAULT_PORT = 8781
DEFAULT_USAGE_PORT = 8779
DEFAULT_SECTION_CHARS = 6000


class SettingsError(ValueError):
    """The settings file is missing or wrong; the message names what and where."""


@dataclass(frozen=True, slots=True)
class KindSettings:
    directory: Path
    owner_first_name: str = ""


@dataclass(frozen=True, slots=True)
class UsageSettings:
    """Where the usage dashboard finds each tool's ledger folder and spend-cap settings file."""

    port: int
    ledgers: dict[str, Path]
    tool_settings: dict[str, Path]


@dataclass(frozen=True, slots=True)
class ClaimsSettings:
    """`[claims]`: the model, the section size and the client's folder (cache and ledgers)."""

    model: str
    section_chars: int
    workspace: Path


@dataclass(frozen=True, slots=True)
class ConceptsSettings:
    """`[concepts]`: the model, the gate and the batch size of the concept pass."""

    model: str
    min_sections: int
    min_claims: int
    batch_claims: int


@dataclass(frozen=True, slots=True)
class MomentsSettings:
    """`[moments]`: the model and the most moments one source may get."""

    model: str
    max_moments: int


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    database: Path
    port: int
    kinds: dict[str, KindSettings]
    path: Path
    usage: UsageSettings | None = None
    claims: ClaimsSettings | None = None
    llm: dict[str, float] = field(default_factory=dict)
    concepts: ConceptsSettings | None = None
    moments: MomentsSettings | None = None

    def client_config(self) -> dict:
        """What `corpuslm.llm.Client` reads: the `[claims]` stage section and the `[llm]` caps."""
        config: dict = {"llm": dict(self.llm)}
        if self.claims is not None:
            config["claims"] = {
                "model": self.claims.model,
                "section_chars": self.claims.section_chars,
            }
        if self.moments is not None:
            config["moments"] = {
                "model": self.moments.model,
                "max_moments": self.moments.max_moments,
            }
        if self.concepts is not None:
            config["concepts"] = {
                "model": self.concepts.model,
                "min_sections": self.concepts.min_sections,
                "min_claims": self.concepts.min_claims,
                "batch_claims": self.concepts.batch_claims,
            }
        return config


def _strict(table: dict, known: tuple[str, ...], where: str) -> None:
    for key in table:
        if key not in known:
            raise SettingsError(f"{where}: unknown key {key!r}; known: {', '.join(known)}")


def _text(table: dict, key: str, where: str, *, required: bool = True) -> str:
    value = table.get(key)
    if value is None:
        if required:
            raise SettingsError(f"{where}: {key} is missing")
        return ""
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{where}: {key} must be a non-empty text")
    return value


def _port(table: dict, where: str, default: int) -> int:
    port = table.get("port", default)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise SettingsError(f"{where}: port must be a whole number from 1024 to 65535")
    return port


def _paths(table: object, where: str, *, required: bool) -> dict[str, Path]:
    if table is None and not required:
        return {}
    if not isinstance(table, dict) or (required and not table):
        raise SettingsError(f'{where}: name at least one tool as `tool = "/path"`')
    return {name: Path(_text(table, name, where)).expanduser() for name in table}


def _usage(raw: object, where: str) -> UsageSettings:
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: must be a table")
    _strict(raw, ("port", "ledgers", "tool_settings"), where)
    ledgers = _paths(raw.get("ledgers"), f"{where}.ledgers", required=True)
    tool_settings = _paths(raw.get("tool_settings"), f"{where}.tool_settings", required=False)
    for name in tool_settings:
        if name not in ledgers:
            raise SettingsError(
                f"{where}.tool_settings: {name!r} has no ledger; known: {', '.join(ledgers)}"
            )
    return UsageSettings(_port(raw, where, DEFAULT_USAGE_PORT), ledgers, tool_settings)


def _claims(raw: object, where: str) -> ClaimsSettings:
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: must be a table")
    _strict(raw, ("model", "section_chars", "workspace"), where)
    size = raw.get("section_chars", DEFAULT_SECTION_CHARS)
    if isinstance(size, bool) or not isinstance(size, int) or size < 500:
        raise SettingsError(f"{where}: section_chars must be a whole number of at least 500")
    return ClaimsSettings(
        _text(raw, "model", where), size, Path(_text(raw, "workspace", where)).expanduser()
    )


def _concepts(raw: object, where: str) -> ConceptsSettings:
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: must be a table")
    _strict(raw, ("model", "min_sections", "min_claims", "batch_claims"), where)
    numbers = {}
    for name, default, least in (
        ("min_sections", 3, 1),
        ("min_claims", 5, 1),
        ("batch_claims", 120, 10),
    ):
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            raise SettingsError(f"{where}: {name} must be a whole number of at least {least}")
        numbers[name] = value
    return ConceptsSettings(_text(raw, "model", where), **numbers)


def _moments(raw: object, where: str) -> MomentsSettings:
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: must be a table")
    _strict(raw, ("model", "max_moments"), where)
    most = raw.get("max_moments", 8)
    if isinstance(most, bool) or not isinstance(most, int) or not 1 <= most <= 50:
        raise SettingsError(f"{where}: max_moments must be a whole number from 1 to 50")
    return MomentsSettings(_text(raw, "model", where), most)


def _llm(raw: object, where: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: must be a table")
    _strict(raw, ("max_usd_per_run", "max_usd_per_day"), where)
    out: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise SettingsError(f"{where}: {key} must be a positive number of dollars")
        out[key] = float(value)
    return out


def load(path: Path) -> ReviewSettings:
    """Read and check the settings file at `path`."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise SettingsError(
            f"{path} does not exist; pass --config FILE (see scripts/review.example.toml)"
        )
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"{path}: not valid TOML ({error})") from error
    _strict(
        raw,
        ("database", "port", "kinds", "usage", "claims", "llm", "concepts", "moments"),
        str(path),
    )
    port = _port(raw, str(path), DEFAULT_PORT)
    kinds_table = raw.get("kinds")
    if not isinstance(kinds_table, dict) or not kinds_table:
        raise SettingsError(f"{path}: name at least one source kind under [kinds.<kind>]")
    kinds: dict[str, KindSettings] = {}
    for name, table in kinds_table.items():
        where = f"{path} [kinds.{name}]"
        if name not in KINDS:
            raise SettingsError(
                f"{where}: no review kind for source kind {name!r}; known: {', '.join(KINDS)}"
            )
        if not isinstance(table, dict):
            raise SettingsError(f"{where}: must be a table")
        _strict(table, ("directory", "owner_first_name"), where)
        kinds[name] = KindSettings(
            Path(_text(table, "directory", where)).expanduser(),
            _text(table, "owner_first_name", where, required=False),
        )
    usage = _usage(raw["usage"], f"{path} [usage]") if "usage" in raw else None
    claims = _claims(raw["claims"], f"{path} [claims]") if "claims" in raw else None
    caps = _llm(raw["llm"], f"{path} [llm]") if "llm" in raw else {}
    concepts = _concepts(raw["concepts"], f"{path} [concepts]") if "concepts" in raw else None
    moments = _moments(raw["moments"], f"{path} [moments]") if "moments" in raw else None
    return ReviewSettings(
        Path(_text(raw, "database", str(path))).expanduser(),
        port,
        kinds,
        path,
        usage,
        claims,
        caps,
        concepts,
        moments,
    )


def kind_setups(settings: ReviewSettings) -> dict[str, KindSetup]:
    """Each source kind's review kind and the folder of its per-source documents."""
    kinds = kinds_for_settings(
        {name: kind_settings.owner_first_name for name, kind_settings in settings.kinds.items()}
    )
    return {name: KindSetup(kinds[name], settings.kinds[name].directory) for name in kinds}


__all__ = [
    "ClaimsSettings",
    "ConceptsSettings",
    "DEFAULT_PORT",
    "MomentsSettings",
    "DEFAULT_SECTION_CHARS",
    "DEFAULT_USAGE_PORT",
    "KindSettings",
    "ReviewSettings",
    "SettingsError",
    "UsageSettings",
    "kind_setups",
    "load",
]
