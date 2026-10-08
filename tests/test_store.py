"""The Repository (`src/corpuslm/store.py`; PIP-2; docs/design/storage-schema.md;
docs/design/pipeline-and-surfaces.md, "Storage: hybrid SQLite catalog…"
(was D10)). tests/test_db.py holds the blind tests of the schema and the
decision tables; this module covers the Store's own lookups: corpus names
without regard to case or outer spaces, the run locks, and the counts the
status board reads."""

import pytest

from corpuslm import db
from corpuslm.store import Store

VID = "vidAAAAAAA1"


@pytest.fixture
def store(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    yield Store(database)
    database.close()


def _corpus(store, name):
    return store.add_corpus(
        {
            "name": name,
            "person": "",
            "subject": "",
            "topic": [],
            "perspective": [],
            "auto_ingest": 0,
            "llm_cleanup": 1,
        },
        created_by="cli",
    )


def test_a_corpus_name_is_found_whatever_its_case_and_outer_spaces(store):
    """Fixture class "duplicate": "Dharma" and " dharma " are one corpus
    (docs/design/corpus-model.md)."""
    cid = _corpus(store, "Dharma")
    assert store.corpus_by_name(" dharma ")["id"] == cid
    assert store.corpus_by_name("DHARMA")["id"] == cid
    assert store.corpus_by_name("dharma", exact=True) is None


def test_a_lock_is_taken_refreshed_by_its_holder_and_released_only_by_it(store):
    store.take_lock("run", "h1", acquired_at="t0", expires_at="t1")
    assert store.lock_holder("run") == {"holder": "h1", "expires_at": "t1"}
    assert store.refresh_locks("h1", expires_at="t2") == 1
    assert store.refresh_locks("h2", expires_at="t9") == 0
    store.release_lock("run", "h2")  # not the holder: the lock stays
    assert store.lock_holder("run")["expires_at"] == "t2"
    store.take_lock("run", "h3", acquired_at="t3", expires_at="t4")  # a takeover
    assert store.lock_holder("run")["holder"] == "h3"
    store.release_lock("run", "h3")
    assert store.lock_holder("run") is None


def test_counts_by_an_unknown_column_are_declined(store):
    store.add_source(VID, "u", created_by="cli")
    # a new video is `added` (docs/design/storage-schema.md, table videos)
    assert store.source_counts_by("status") == {"added": 1}
    with pytest.raises(ValueError, match="title"):
        store.source_counts_by("title")


def test_mark_stale_reports_whether_there_was_an_artifact(store):
    store.add_source(VID, "u", created_by="cli")
    assert store.mark_stale(VID, "pass3") is False
    store.record_artifact(
        VID,
        "pass3",
        path="p",
        sha256="a" * 64,
        state="built",
        inputs={},
        provenance={"ambiguities": 3},
        created_by="cli",
    )
    assert store.artifact_health()["built"] == {"pass3": 1}
    assert store.mark_stale(VID, "pass3") is True
    health = store.artifact_health()
    assert health["stale"] == 1 and health["built"] == {} and health["ambiguities"] == 3


def _video(store, source_id):
    store.add_source(source_id, f"https://youtu.be/{source_id}", created_by="cli")


def test_merging_corpora_moves_every_membership_and_keeps_each_folded_corpus_whole(store):
    """`merge_corpora` folds corpora into one in one transaction: memberships
    move (a video already in the target keeps the target's row), each folded
    corpus's row, lists and members are kept under the target's
    `metadata.merged_from` (written to the change log), and the folded rows go."""
    a, b, into = (_corpus(store, n) for n in ("Physics/A", "Physics/B", "Physics"))
    store.update_corpus(a, {"topic": ["Entropy"], "person": "Ann"})
    for vid in ("vidAAAAAAA1", "vidAAAAAAA2", "vidAAAAAAA3"):
        _video(store, vid)
    moved_member = store.add_member(a, "vidAAAAAAA1", "author", created_by="cli")
    store.add_member(b, "vidAAAAAAA2", "discovered", created_by="cli")
    store.add_member(b, "vidAAAAAAA3", "author", created_by="cli")
    store.add_member(into, "vidAAAAAAA3", "author", created_by="cli")
    store.record_artifact(
        a, "corpus_note", path="n.md", sha256="0" * 64, state="built",
        inputs={}, provenance={}, created_by="cli",
    )  # fmt: skip

    board = store.merge_corpora(into, [a, b], created_by="cli")

    assert board == {"moved": 2, "already_members": 1, "removed": ["Physics/A", "Physics/B"]}
    assert store.corpus(a) is None and store.corpus(b) is None
    assert store.artifact(a, "corpus_note") is None
    assert sorted(store.member_source_ids(into)) == ["vidAAAAAAA1", "vidAAAAAAA2", "vidAAAAAAA3"]
    row = store.db.one("SELECT * FROM corpus_members WHERE id = ?", (moved_member,))
    assert row["corpus_id"] == into and row["reason"] == "author"
    assert db.loads(row["metadata"], {})["merged_from"] == a
    kept = db.loads(store.corpus(into)["metadata"], {})["merged_from"]
    assert [k["name"] for k in kept] == ["Physics/A", "Physics/B"]
    assert kept[0]["topic"] == ["Entropy"]
    assert kept[0]["person"] == "Ann" and kept[0]["id"] == a
    assert [m["source_id"] for m in kept[1]["members"]] == ["vidAAAAAAA2", "vidAAAAAAA3"]
    assert kept[1]["members"][0]["reason"] == "discovered"
    logged = [c["column_name"] for c in store.changes_of("corpora", into)]
    assert "metadata" in logged


def test_a_corpus_is_not_merged_into_itself(store):
    a = _corpus(store, "A")
    with pytest.raises(ValueError, match="into itself"):
        store.merge_corpora(a, [a], created_by="cli")
    assert store.corpus(a) is not None


def test_dropping_a_videos_transcript_rows_empties_its_segments_index_and_named_artifacts(store):
    """The derived state of one video goes whole (ytlm `video drop-transcript`):
    its segments and their FTS5 rows (the segments_ad trigger), and the
    artifact rows of the nodes named; another video's rows and the nodes not
    named are left alone."""
    other = "vidBBBBBBB2"
    for vid in (VID, other):
        store.add_source(vid, "u", created_by="cli")
        store.replace_segments(vid, [{"idx": 0, "t0": 0.0, "t1": 1.0, "text": "namaste dharma"}])
        for node in ("metadata", "captions", "pass3"):
            store.record_artifact(
                vid, node, path=f"videos/{vid}/{node}", sha256="a" * 64, state="built",
                inputs={}, provenance={}, created_by="cli",
            )  # fmt: skip
    assert store.segment_count(VID) == 1 and store.segment_count() == 2
    assert [a["node"] for a in store.artifacts_of(VID)] == ["captions", "metadata", "pass3"]

    assert store.delete_segments(VID) == 1
    assert store.delete_artifacts(VID, ["captions", "pass3", "srt"]) == 2

    assert store.segment_count(VID) == 0 and store.segment_count(other) == 1
    assert [h["source_id"] for h in store.search("dharma", corpus_id=None, limit=5)] == [other]
    assert [a["node"] for a in store.artifacts_of(VID)] == ["metadata"]
    assert len(store.artifacts_of(other)) == 3
