"""Speaker map (`src/corpuslm/speaker_map.py`): candidates from a source's vocabulary,
label-to-name bindings with anchors, statuses RESOLVED_FROM_METADATA | INFERRED | AUTHOR |
NEEDS_AUTHOR. Run over a YouTube-shaped scheme and Bee's. Synthetic text only."""

import pytest
from schemes import LABELS, OWNER, SCHEMES, SOURCE

from corpuslm import speaker_map


@pytest.fixture(params=["youtube", "bee"])
def kind(request):
    return request.param


@pytest.fixture
def scheme(kind):
    return SCHEMES[kind]


def _labels_document(labels):
    return {
        "segments": [
            {"idx": segment_index, "speaker": label, "role": label, "text": f"line {segment_index}"}
            for segment_index, label in enumerate(labels)
        ]
    }


def test_the_default_label_resolves_and_a_confident_binding_is_inferred(kind, scheme):
    first, second = LABELS[kind]
    document = _labels_document([first, second, first, second])
    bindings = [{"role": second, "name": "Ann Example", "idx": 1, "anchor": "line 1"}]
    speaker_map_document = speaker_map.build_map(
        SOURCE, document, bindings, candidates=["Some Channel", "Ann Example"], scheme=scheme
    )
    resolved = "Some Channel" if kind == "youtube" else OWNER
    assert speaker_map_document["roles"][first] == {
        "name": resolved,
        "status": "RESOLVED_FROM_METADATA",
    }
    assert speaker_map_document["roles"][second]["name"] == "Ann Example"
    assert speaker_map_document["roles"][second]["status"] == "INFERRED"
    assert speaker_map_document["roles"][second]["anchor"] == "line 1"
    applied_document = speaker_map.apply_map(document, speaker_map_document, scheme=scheme)
    assert applied_document["segments"][1]["speaker"] == "Ann Example"
    assert applied_document["segments"][0]["speaker"] == resolved
    assert speaker_map.display_names(speaker_map_document) == {
        first: resolved,
        second: "Ann Example",
    }


def test_a_label_with_no_binding_or_a_conflict_needs_the_owner(kind, scheme):
    first, second = LABELS[kind]
    third = "GUEST_2" if kind == "youtube" else "Unknown"
    document = _labels_document([first, second, third])
    bindings = [
        {"role": second, "name": "Ann", "idx": 1, "anchor": "line 1"},
        {"role": second, "name": "Anne Other", "idx": 2, "anchor": "line 2"},
    ]
    speaker_map_document = speaker_map.build_map(
        SOURCE, document, bindings, candidates=["Some Channel"], scheme=scheme
    )
    assert speaker_map_document["roles"][second]["status"] == "NEEDS_AUTHOR"
    assert speaker_map_document["roles"][second]["key"] == "spk-1"
    assert "Ann (line 1)" in speaker_map_document["roles"][second]["anchor"]
    assert speaker_map_document["roles"][third]["status"] == "NEEDS_AUTHOR"
    assert (
        speaker_map_document["roles"][third]["key"] == "spk-2"
        and speaker_map_document["roles"][third]["name"] == ""
    )
    applied_document = speaker_map.apply_map(document, speaker_map_document, scheme=scheme)
    assert applied_document["segments"][1]["speaker"] == second  # the label is used meanwhile
    assert sorted(speaker_map.review_items(speaker_map_document)) == ["spk-1", "spk-2"]


def test_a_weak_binding_is_a_question_for_the_owner_not_a_fact(kind, scheme):
    first, second = LABELS[kind]
    document = _labels_document([first, second])
    weak = [{"role": second, "name": "Ann", "idx": 1, "anchor": "line 1", "confidence": 0.3}]
    speaker_map_document = speaker_map.build_map(
        SOURCE, document, weak, candidates=[], scheme=scheme
    )
    assert speaker_map_document["roles"][second]["status"] == "NEEDS_AUTHOR"
    assert speaker_map_document["roles"][second]["anchor"] == "Ann (line 1)"


def test_an_owner_verdict_outranks_everything(kind, scheme):
    first, second = LABELS[kind]
    speaker_map_document = speaker_map.build_map(
        SOURCE,
        _labels_document([first, second]),
        [],
        candidates=[],
        scheme=scheme,
        owner_names={first: "Zed"},
    )
    assert speaker_map_document["roles"][first] == {"name": "Zed", "status": "AUTHOR"}


def test_the_bee_scheme_resolves_only_the_owner_label():
    scheme = SCHEMES["bee"]
    assert scheme.fixed_labels and scheme.unresolved_label is None
    assert scheme.resolve_default(OWNER, SOURCE) == OWNER
    for other in ("Unknown", "Speaker 2", "Speaker 3", ""):
        assert scheme.resolve_default(other, SOURCE) is None
    assert speaker_map.bee_scheme("Ann").resolve_default(OWNER, SOURCE) is None  # not hard-coded
    assert speaker_map.bee_scheme("").resolve_default("", SOURCE) is None
    document = _labels_document(["Unknown", OWNER, "Speaker 2", "Speaker 3", OWNER])
    speaker_map_document = speaker_map.build_map(SOURCE, document, [], candidates=[], scheme=scheme)
    assert list(speaker_map_document["roles"]) == [
        "Unknown",
        OWNER,
        "Speaker 2",
        "Speaker 3",
    ]  # first seen
    assert speaker_map_document["roles"][OWNER]["status"] == "RESOLVED_FROM_METADATA"
    assert speaker_map.review_items(speaker_map_document) == ["spk-1", "spk-2", "spk-3"]
    assert speaker_map_document["source_id"] == "src-1" and "video_id" not in speaker_map_document
    applied_document = speaker_map.apply_map(document, speaker_map_document, scheme=scheme)
    assert [segment["speaker"] for segment in applied_document["segments"]] == [
        "Unknown",
        OWNER,
        "Speaker 2",
        "Speaker 3",
        OWNER,
    ]


def test_the_unresolved_label_has_no_speaker_and_no_map_entry_when_the_scheme_names_one():
    scheme = SCHEMES["youtube"]
    document = _labels_document(["HOST", "UNKNOWN"])
    speaker_map_document = speaker_map.build_map(SOURCE, document, [], candidates=[], scheme=scheme)
    assert (
        list(speaker_map_document["roles"]) == ["HOST"]
        and speaker_map_document["video_id"] == "src-1"
    )
    assert (
        speaker_map.apply_map(document, speaker_map_document, scheme=scheme)["segments"][1][
            "speaker"
        ]
        == ""
    )
    assert speaker_map.labels_present(
        _labels_document(["GUEST_2", "HOST", "ODD"]), scheme=scheme
    ) == ["HOST", "GUEST_2"]


def test_candidates_are_deduplicated_in_order(scheme):
    speaker_map_document = speaker_map.build_map(
        SOURCE, _labels_document([]), [], candidates=["b", "a", "b"], scheme=scheme
    )
    assert speaker_map_document["candidates"] == ["b", "a"]


def test_the_shared_speaker_map_schema_takes_either_source_id_key_and_names_a_bad_status():
    from corpuslm import schemas

    for key in ("video_id", "source_id"):
        document = {
            key: "s1",
            "candidates": [],
            "roles": {"A": {"name": "", "status": "NEEDS_AUTHOR"}},
        }
        schemas.validate("speaker_map", document)
    with pytest.raises(schemas.SchemaError, match="status"):
        schemas.validate(
            "speaker_map",
            {"source_id": "s1", "candidates": [], "roles": {"A": {"name": "", "status": "MAYBE"}}},
        )
    with pytest.raises(schemas.SchemaError):
        schemas.validate("speaker_map", {"candidates": [], "roles": {}})
