"""Events (`src/corpuslm/events.py`): what corpuslm publishes, after its own commit, to the
common events database, through `eventlm`'s fake. No real events file is ever touched."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from eventlm.events import Events
from test_directives import MODEL, REPLY, SEGMENTS, SOURCE

from corpuslm import claims, db, events, llm, review_apply
from corpuslm.store import Store


def _claims_world(tmp_path):
    database = db.Database(tmp_path / "transcript.sqlite")
    store = Store(database)
    store.add_source(SOURCE, "l", created_by="test", kind="bee")
    store.replace_segments(SOURCE, SEGMENTS)
    folder = tmp_path / "scripts" / claims.STAGE
    folder.mkdir(parents=True)
    (folder / f"{SOURCE}-s0.json").write_text(
        json.dumps({"reply": json.dumps(REPLY), "tokens_in": 100, "tokens_out": 20})
    )
    client = llm.client(
        tmp_path / "ws",
        {"claims": {"model": MODEL, "section_chars": 10_000}, "llm": {}},
        transport=llm.ScriptedTransport(tmp_path / "scripts"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    return database, store, client


def test_claims_built_is_published_after_the_claims_are_committed(tmp_path):
    database, store, client = _claims_world(tmp_path)
    bus = Events.fake()
    try:
        seen_at_publish = {}
        real_publish = bus.publish

        def publish(producer, kind, subject=None, facts=None):
            # the rows are there when the event is appended: the transaction has committed
            seen_at_publish["claims"] = store.claim_count(SOURCE)
            seen_at_publish["in_transaction"] = database.conn.in_transaction
            return real_publish(producer, kind, subject, facts)

        bus.publish = publish  # type: ignore[method-assign]
        out = claims.build(store, client, SOURCE, created_by="test", events=bus)
        (event,) = bus.tail()
        assert (event.producer, event.kind) == ("corpuslm", "claims.built")
        assert event.subject == {"source": SOURCE}
        assert event.facts == {"claims": out["claims"], "directives": out["directives"]}
        assert seen_at_publish == {"claims": 4, "in_transaction": False}
        assert "statement" not in json.dumps(event.as_dict())  # ids and counts, never content
    finally:
        bus.close()
        database.close()


def test_without_an_events_store_the_pass_publishes_nothing_and_still_builds(tmp_path):
    database, store, client = _claims_world(tmp_path)
    try:
        out = claims.build(store, client, SOURCE, created_by="test")
        assert out["claims"] == 4 and store.claim_count(SOURCE) == 4
        assert events.publish(None, events.CLAIMS_BUILT, {"source": SOURCE}) is None
    finally:
        database.close()


def test_speaker_landed_is_published_once_per_source_after_apply(tmp_path, monkeypatch):
    from review_kinds import (
        BEE_KIND,
        BEE_SOURCE,
        OWNER,
        bee_speaker_map,
        bee_transcript,
        write_source,
    )

    from corpuslm import review_page
    from corpuslm.review_page import KindSetup

    database = db.Database(tmp_path / "transcript.sqlite")
    bus = Events.fake()
    try:
        store = Store(database)
        corpus = store.add_corpus({"name": "Walks"}, created_by="test")
        store.add_source(BEE_SOURCE, "transcripts/x.md", created_by="test", kind="bee")
        store.add_member(corpus, BEE_SOURCE, "author", created_by="test")
        kinds = {"bee": KindSetup(BEE_KIND, tmp_path / "transcripts")}
        write_source(tmp_path / "transcripts", BEE_SOURCE, bee_transcript(), bee_speaker_map())
        cards = review_page.page_data_by_kind(database, kinds, "Walks")["cards"]
        speaker = next(card for card in cards if card["kind"] == "speaker")
        store.save_review_answer(
            corpus, speaker["id"], OWNER, "2026-10-09T10:00:00Z", created_by="test"
        )
        rebuilt = []
        report = review_apply.apply(
            database,
            kinds,
            created_by="test",
            rebuilders={"bee": lambda sources: rebuilt.append(sorted(sources)) or 1},
            events=bus,
        )
        assert report.verdicts_landed == 1 and rebuilt == [[BEE_SOURCE]]
        (event,) = bus.tail()
        assert (event.kind, event.subject) == ("speaker.landed", {"source": BEE_SOURCE})
        assert event.facts == {"kind": "bee", "rebuilt": True}
        # a dry run publishes nothing
        review_apply.apply(database, kinds, created_by="test", dry_run=True, events=bus)
        assert len(bus.tail()) == 1
    finally:
        bus.close()
        database.close()


def test_a_missing_pointer_is_a_note_and_nothing_fails(tmp_path, monkeypatch):
    monkeypatch.delenv(events.POINTER, raising=False)
    assert events.pointer(tmp_path / ".env") is None
    store, note = events.open_events(tmp_path / ".env")
    assert store is None and note is not None and "EVENTS_DB is not set" in note
    (tmp_path / ".env").write_text("OTHER=1\nEVENTS_DB=events.sqlite\n")
    assert events.pointer(tmp_path / ".env") == tmp_path.cwd() / "events.sqlite" or (
        events.pointer(tmp_path / ".env").name == "events.sqlite"
    )
    (tmp_path / ".env").write_text(f'EVENTS_DB="{tmp_path / "missing.sqlite"}"\n')
    store, note = events.open_events(tmp_path / ".env")
    assert store is None and note is not None and "eventlm init" in note
    monkeypatch.setenv(events.POINTER, str(tmp_path / "from-env.sqlite"))
    assert events.pointer(tmp_path / ".env") == tmp_path / "from-env.sqlite"  # the environment wins
    Events.init(tmp_path / "from-env.sqlite").close()
    store, note = events.open_events()
    assert store is not None and note is None
    store.close()
