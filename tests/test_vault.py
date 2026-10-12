"""Vault (corpuslm.vault): the marker rules, speaker properties, transcript lines and the corpus
note, for a kind with a marker of its own and a source with no link."""

from __future__ import annotations

import pytest
from review_kinds import BEE_KIND, BEE_SOURCE, LINKED_KIND, bee_speaker_map, bee_transcript

from corpuslm import vault

MARKER = "beelm: exported"


def test_note_name_is_safe_for_a_filename():
    assert vault.note_name('a/b:c*d?"e<f>g|h#i^j[k]l') == "a-b-c-d--e-f-g-h-i-j-k-l"
    assert vault.note_name("  ... ") == "untitled"
    assert vault.note_name("Walk to the kiln.") == "Walk to the kiln"


def test_write_note_leaves_an_unmarked_note_at_the_target_alone(tmp_path):
    (tmp_path / "walk.md").write_text("my own note", encoding="utf-8")
    with pytest.raises(vault.UnmarkedNoteInTheWay):
        vault.write_note(tmp_path, "walk.md", f"---\n{MARKER}\n---\n", "id", "1", marker=MARKER)
    assert (tmp_path / "walk.md").read_text(encoding="utf-8") == "my own note"


def test_write_note_replaces_its_own_and_removes_the_old_name_of_the_same_source(tmp_path):
    first = f"---\n{MARKER}\nsource_id: s1\n---\nold\n"
    vault.write_note(tmp_path, "old name.md", first, "source_id", "s1", marker=MARKER)
    vault.write_note(
        tmp_path, "new name.md", first.replace("old", "new"), "source_id", "s1", marker=MARKER
    )
    assert [path.name for path in tmp_path.glob("*.md")] == ["new name.md"]
    # another kind's marker is not ours to remove
    (tmp_path / "other.md").write_text(
        "---\nytlm: exported\nsource_id: s1\n---\n", encoding="utf-8"
    )
    assert vault.remove_exported(tmp_path, "source_id", "s1", marker=MARKER) == 1
    assert [path.name for path in tmp_path.glob("*.md")] == ["other.md"]


def test_speaker_properties_are_one_flat_key_per_label():
    lines = vault.speaker_properties(
        bee_speaker_map(unknown_status="AUTHOR", unknown_name='Mo "ss"')
    )
    assert lines == ['speaker_Quill: "Quill"', 'speaker_Unknown: "Mo \\"ss\\""']
    assert vault.speaker_properties(None) == [] and vault.speaker_properties({"roles": {}}) == []


def test_transcript_lines_without_a_link_show_the_time_alone():
    source = {"id": BEE_SOURCE}
    lines = vault.transcript_lines(bee_transcript(), source, BEE_KIND.anchor_url)
    assert lines[0] == "- 0:00 **Quill**: Quill here, notes from the walk."
    assert all("](" not in line for line in lines)
    linked = vault.transcript_lines(bee_transcript(), source, LINKED_KIND.anchor_url)
    assert linked[1].startswith(f"- [0:05](https://example.test/{BEE_SOURCE}?t=5s) **Unknown**: ")


def test_the_corpus_note_lists_members_with_wikilinks_under_the_kinds_marker():
    corpus = {"id": "cor-1", "name": "Walks", "person": "Quill", "topic": ["kilns"]}
    members = [{"id": "s1", "title": "Kiln walk", "member_status": "member"}, {"id": "s2"}]
    text = vault.render_corpus_note(
        corpus,
        members,
        marker=MARKER,
        source_label=BEE_KIND.source_label,
        member_details=lambda member: member.get("member_status", "?"),
    )
    assert text.startswith(f'---\n{MARKER}\ncorpus_id: cor-1\nname: "Walks"\nperson: "Quill"\n')
    assert 'topic:\n  - "kilns"\n' in text
    assert "- [[Kiln walk (s1)|Kiln walk]] — member" in text
    assert "- [[s2 (s2)|s2]] — ?" in text
