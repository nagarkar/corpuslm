"""The document store interface and its file-backed fake (corpuslm.document): what a
revision means (`apply_revision`), the store selection, and the fake's stage and resolve
rules that consumers' tests rely on."""

from pathlib import Path

import pytest

from corpuslm import document as authorlm


def test_a_replacement_swaps_one_paragraph_and_an_addition_lands_after_its_place():
    text = "# T\n\nOne.\n\nTwo.\n"
    assert authorlm.apply_revision(text, {"old": "One.", "new": "Uno.", "after": 0}) == (
        "# T\n\nUno.\n\nTwo.\n"
    )
    assert authorlm.apply_revision(text, {"old": "", "new": "New.", "after": 2}) == (
        "# T\n\nOne.\n\nNew.\n\nTwo.\n"
    )
    assert authorlm.apply_revision(text, {"old": "", "new": "End.", "after": 99}).endswith(
        "Two.\n\nEnd.\n"
    )


def test_replacing_a_paragraph_that_is_not_there_is_an_error():
    with pytest.raises(ValueError, match="not in the tab"):
        authorlm.apply_revision("A.\n", {"old": "B.", "new": "C.", "after": 0})


def test_the_fake_is_chosen_in_process_and_the_real_store_needs_a_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("corpuslm.document.FAKE_DIR", Path(tmp_path / "docs"))
    assert isinstance(authorlm.store(), authorlm.FakeDocumentStore)
    monkeypatch.setattr("corpuslm.document.FAKE_DIR", None)
    with pytest.raises(authorlm.DocumentUnavailable, match="workspace"):
        authorlm.store()


def test_a_tab_with_open_revisions_takes_no_more_and_says_why_for_each(tmp_path):
    store = authorlm.FakeDocumentStore(tmp_path)
    assert store.ensure("C", tmp_path / "c", "C")["created"] is True
    assert store.ensure("C", tmp_path / "c", "C")["created"] is False
    store.add_tab("C", "a.md", "A")
    store.add_tab("C", "a.md", "A")  # a duplicate add is a no-op
    assert [tab["file"] for tab in store.read("C")["tabs"]] == ["a.md"]
    first = store.stage("C", "a.md", [{"old": "", "new": "One.", "after": 1, "note": ""}])
    assert first["written"] == 1 and first["failed"] == []
    second = store.stage("C", "a.md", [{"old": "", "new": "Two.", "after": 1, "note": ""}] * 2)
    assert second["written"] == 0
    assert [failure["index"] for failure in second["failed"]] == [0, 1]
    assert {failure["reason"] for failure in second["failed"]} == {"the tab has open revisions"}


def test_resolve_lands_accepted_and_modified_and_returns_declined(tmp_path):
    store = authorlm.FakeDocumentStore(tmp_path)
    store.ensure("C", tmp_path / "c", "C")
    store.add_tab("C", "a.md", "A")
    store.stage(
        "C",
        "a.md",
        [
            {"old": "", "new": "Kept.", "after": 1, "note": ""},
            {"old": "", "new": "Dropped.", "after": 1, "note": ""},
        ],
    )
    import json

    path = tmp_path / "C.json"
    manifest = json.loads(path.read_text())
    manifest["tabs"][0]["open"][1]["verdict"] = "decline"
    path.write_text(json.dumps(manifest))
    counts = store.resolve("C", "a.md")
    assert (counts["accepted"], counts["modified"], counts["declined"]) == (1, 0, 1)
    assert counts["declined_revisions"][0]["new"] == "Dropped."
    assert authorlm.paragraphs(store.read("C")["tabs"][0]["settled"]) == ["# A", "Kept."]


def test_an_exported_switch_reaches_nothing(tmp_path, monkeypatch):
    """The fake comes in by injection, not by environment variable, so one
    left exported in a shell or a launchd plist changes nothing in a real
    run (corpuslm.document)."""
    monkeypatch.setattr("corpuslm.document.FAKE_DIR", None)
    for name in ("CORPUSLM_DOCUMENT_FAKE", "YTLM_DOCUMENT_FAKE"):
        monkeypatch.setenv(name, str(tmp_path / name))
    with pytest.raises(authorlm.DocumentUnavailable, match="workspace"):
        authorlm.store(workspace=None)
