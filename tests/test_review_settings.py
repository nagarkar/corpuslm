"""Review settings (corpuslm.review_settings): the one small file the review server reads."""

from __future__ import annotations

import pytest

from corpuslm import review_settings
from corpuslm.review_settings import SettingsError

GOOD = """
database = "{database}"
port = 8999

[kinds.youtube]
directory = "{folder}/videos"

[kinds.bee]
directory = "{folder}/transcripts"
owner_first_name = "Quill"
"""


def write(tmp_path, text=GOOD):
    path = tmp_path / "review.toml"
    path.write_text(text.format(database=tmp_path / "transcript.sqlite", folder=tmp_path))
    return path


def test_the_database_path_comes_from_the_file(tmp_path):
    settings = review_settings.load(write(tmp_path))
    assert settings.database == tmp_path / "transcript.sqlite"
    assert settings.port == 8999
    assert settings.kinds["bee"].owner_first_name == "Quill"
    assert settings.kinds["youtube"].directory == tmp_path / "videos"
    setups = review_settings.kind_setups(settings)
    assert setups["bee"].sources_directory == tmp_path / "transcripts"
    assert setups["youtube"].kind.name == "youtube"


def test_the_port_has_a_default(tmp_path):
    text = GOOD.replace("port = 8999\n", "")
    assert review_settings.load(write(tmp_path, text)).port == review_settings.DEFAULT_PORT


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (("port = 8999", "port = 8999\nspeed = 3"), "unknown key 'speed'"),
        (("port = 8999", 'port = "8999"'), "port must be a whole number"),
        (("[kinds.bee]", "[kinds.podcast]"), "no review kind for source kind 'podcast'"),
        (('directory = "{folder}/videos"', ""), "directory is missing"),
        (('database = "{database}"', ""), "database is missing"),
        (('owner_first_name = "Quill"', "owner = 1"), "unknown key 'owner'"),
    ],
)
def test_an_unknown_key_is_named_and_declined(tmp_path, edit, message):
    old, new = edit
    path = write(tmp_path, GOOD.replace(old, new))
    with pytest.raises(SettingsError, match=message):
        review_settings.load(path)


def test_an_unknown_source_kind_is_named_and_declined(tmp_path):
    path = write(tmp_path, GOOD.replace("[kinds.bee]", "[kinds.audio]"))
    with pytest.raises(SettingsError, match="audio"):
        review_settings.load(path)


def test_a_missing_file_says_how_to_name_one(tmp_path):
    with pytest.raises(SettingsError, match="--config FILE"):
        review_settings.load(tmp_path / "nope.toml")


USAGE = """
[usage]
port = 8790

[usage.ledgers]
ytlm = "{folder}/ytlm-logs"
beelm = "~/Bee/beelm/logs"

[usage.tool_settings]
ytlm = "{folder}/ytlm.toml"
"""


def test_the_usage_table_is_read_and_is_optional(tmp_path):
    assert review_settings.load(write(tmp_path)).usage is None
    usage = review_settings.load(write(tmp_path, GOOD + USAGE)).usage
    assert usage.port == 8790
    assert usage.ledgers["ytlm"] == tmp_path / "ytlm-logs"
    assert usage.ledgers["beelm"].is_absolute()  # ~ is expanded
    assert set(usage.tool_settings) == {"ytlm"}
    assert (
        review_settings.load(write(tmp_path, GOOD + "[usage.ledgers]\nytlm = '/x'\n")).usage.port
        == 8779
    )


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("[usage]\nport = 1\n[usage.ledgers]\nytlm = '/x'\n", "port must be"),
        ("[usage]\nfoo = 1\n", "unknown key 'foo'"),
        ("[usage]\n", "name at least one tool"),
        ("[usage.ledgers]\nytlm = ''\n", "non-empty"),
        ("[usage.ledgers]\nytlm = '/x'\n[usage.tool_settings]\nother = '/y'\n", "no ledger"),
    ],
)
def test_a_wrong_usage_table_is_named(tmp_path, addition, message):
    with pytest.raises(SettingsError, match=message):
        review_settings.load(write(tmp_path, GOOD + addition))


CLAIMS = """
[claims]
model = "gemini/gemini-2.5-flash-lite"
workspace = "{folder}/corpuslm"

[llm]
max_usd_per_run = 1.5
"""


def test_the_claims_and_llm_tables_are_read_and_are_optional(tmp_path):
    bare = review_settings.load(write(tmp_path))
    assert bare.claims is None and bare.llm == {} and bare.client_config() == {"llm": {}}
    settings = review_settings.load(write(tmp_path, GOOD + CLAIMS))
    assert settings.claims.model == "gemini/gemini-2.5-flash-lite"
    assert settings.claims.section_chars == review_settings.DEFAULT_SECTION_CHARS
    assert settings.claims.workspace == tmp_path / "corpuslm"
    assert settings.llm == {"max_usd_per_run": 1.5}
    assert settings.client_config() == {
        "llm": {"max_usd_per_run": 1.5},
        "claims": {"model": "gemini/gemini-2.5-flash-lite", "section_chars": 6000},
    }


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("[claims]\nmodel = 'm'\n", "workspace is missing"),
        ("[claims]\nworkspace = '/x'\n", "model is missing"),
        ("[claims]\nmodel = 'm'\nworkspace = '/x'\nsection_chars = 10\n", "at least 500"),
        ("[claims]\nmodel = 'm'\nworkspace = '/x'\nfoo = 1\n", "unknown key 'foo'"),
        ("[llm]\nmax_usd_per_run = 0\n", "positive number of dollars"),
        ("[llm]\nmax_usd_per_hour = 1\n", "unknown key 'max_usd_per_hour'"),
    ],
)
def test_a_wrong_claims_or_llm_table_is_named(tmp_path, addition, message):
    with pytest.raises(SettingsError, match=message):
        review_settings.load(write(tmp_path, GOOD + addition))


def test_the_concepts_table_is_read_with_its_defaults(tmp_path):
    settings = review_settings.load(write(tmp_path, GOOD + CLAIMS + "[concepts]\nmodel = 'm'\n"))
    assert settings.concepts.model == "m"
    assert (settings.concepts.min_sections, settings.concepts.min_claims) == (3, 5)
    assert settings.concepts.batch_claims == 120
    assert settings.client_config()["concepts"] == {
        "model": "m", "min_sections": 3, "min_claims": 5, "batch_claims": 120
    }  # fmt: skip
    assert review_settings.load(write(tmp_path)).concepts is None


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("[concepts]\nmin_sections = 3\n", "model is missing"),
        ("[concepts]\nmodel = 'm'\nmin_claims = 0\n", "min_claims must be"),
        ("[concepts]\nmodel = 'm'\nbatch_claims = 5\n", "batch_claims must be"),
        ("[concepts]\nmodel = 'm'\nfoo = 1\n", "unknown key 'foo'"),
    ],
)
def test_a_wrong_concepts_table_is_named(tmp_path, addition, message):
    with pytest.raises(SettingsError, match=message):
        review_settings.load(write(tmp_path, GOOD + addition))
