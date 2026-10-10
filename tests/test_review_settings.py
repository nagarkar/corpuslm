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
