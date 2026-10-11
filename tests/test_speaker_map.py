"""Speaker map (`src/corpuslm/speaker_map.py`): candidates from a source's vocabulary,
label-to-name bindings with anchors, statuses RESOLVED_FROM_METADATA | INFERRED | AUTHOR |
NEEDS_AUTHOR. Run over a YouTube-shaped scheme and Bee's. Synthetic text only."""

import pytest
from schemes import LABELS, OWNER, SCHEMES, SOURCE

from corpuslm import speaker_map as sm


@pytest.fixture(params=["youtube", "bee"])
def kind(request):
    return request.param


@pytest.fixture
def scheme(kind):
    return SCHEMES[kind]


def _t(labels):
    return {
        "segments": [
            {"idx": i, "speaker": label, "role": label, "text": f"line {i}"}
            for i, label in enumerate(labels)
        ]
    }


def test_the_default_label_resolves_and_a_confident_binding_is_inferred(kind, scheme):
    first, second = LABELS[kind]
    document = _t([first, second, first, second])
    bindings = [{"role": second, "name": "Ann Example", "idx": 1, "anchor": "line 1"}]
    m = sm.build_map(
        SOURCE, document, bindings, candidates=["Some Channel", "Ann Example"], scheme=scheme
    )
    resolved = "Some Channel" if kind == "youtube" else OWNER
    assert m["roles"][first] == {"name": resolved, "status": "RESOLVED_FROM_METADATA"}
    assert m["roles"][second]["name"] == "Ann Example"
    assert m["roles"][second]["status"] == "INFERRED"
    assert m["roles"][second]["anchor"] == "line 1"
    t2 = sm.apply_map(document, m, scheme=scheme)
    assert t2["segments"][1]["speaker"] == "Ann Example"
    assert t2["segments"][0]["speaker"] == resolved
    assert sm.display_names(m) == {first: resolved, second: "Ann Example"}


def test_a_label_with_no_binding_or_a_conflict_needs_the_owner(kind, scheme):
    first, second = LABELS[kind]
    third = "GUEST_2" if kind == "youtube" else "Unknown"
    document = _t([first, second, third])
    bindings = [
        {"role": second, "name": "Ann", "idx": 1, "anchor": "line 1"},
        {"role": second, "name": "Anne Other", "idx": 2, "anchor": "line 2"},
    ]
    m = sm.build_map(SOURCE, document, bindings, candidates=["Some Channel"], scheme=scheme)
    assert m["roles"][second]["status"] == "NEEDS_AUTHOR"
    assert m["roles"][second]["key"] == "spk-1"
    assert "Ann (line 1)" in m["roles"][second]["anchor"]
    assert m["roles"][third]["status"] == "NEEDS_AUTHOR"
    assert m["roles"][third]["key"] == "spk-2" and m["roles"][third]["name"] == ""
    t2 = sm.apply_map(document, m, scheme=scheme)
    assert t2["segments"][1]["speaker"] == second  # the label is used meanwhile
    assert sorted(sm.review_items(m)) == ["spk-1", "spk-2"]


def test_a_weak_binding_is_a_question_for_the_owner_not_a_fact(kind, scheme):
    first, second = LABELS[kind]
    document = _t([first, second])
    weak = [{"role": second, "name": "Ann", "idx": 1, "anchor": "line 1", "confidence": 0.3}]
    m = sm.build_map(SOURCE, document, weak, candidates=[], scheme=scheme)
    assert m["roles"][second]["status"] == "NEEDS_AUTHOR"
    assert m["roles"][second]["anchor"] == "Ann (line 1)"


def test_an_owner_verdict_outranks_everything(kind, scheme):
    first, second = LABELS[kind]
    m = sm.build_map(
        SOURCE, _t([first, second]), [], candidates=[], scheme=scheme, owner_names={first: "Zed"}
    )
    assert m["roles"][first] == {"name": "Zed", "status": "AUTHOR"}


def test_the_bee_scheme_resolves_only_the_owner_label():
    scheme = SCHEMES["bee"]
    assert scheme.fixed_labels and scheme.unresolved_label is None
    assert scheme.resolve_default(OWNER, SOURCE) == OWNER
    for other in ("Unknown", "Speaker 2", "Speaker 3", ""):
        assert scheme.resolve_default(other, SOURCE) is None
    assert sm.bee_scheme("Ann").resolve_default(OWNER, SOURCE) is None  # not hard-coded
    assert sm.bee_scheme("").resolve_default("", SOURCE) is None
    document = _t(["Unknown", OWNER, "Speaker 2", "Speaker 3", OWNER])
    m = sm.build_map(SOURCE, document, [], candidates=[], scheme=scheme)
    assert list(m["roles"]) == ["Unknown", OWNER, "Speaker 2", "Speaker 3"]  # first seen
    assert m["roles"][OWNER]["status"] == "RESOLVED_FROM_METADATA"
    assert sm.review_items(m) == ["spk-1", "spk-2", "spk-3"]
    assert m["source_id"] == "src-1" and "video_id" not in m
    t2 = sm.apply_map(document, m, scheme=scheme)
    assert [s["speaker"] for s in t2["segments"]] == [
        "Unknown",
        OWNER,
        "Speaker 2",
        "Speaker 3",
        OWNER,
    ]


def test_the_unresolved_label_has_no_speaker_and_no_map_entry_when_the_scheme_names_one():
    scheme = SCHEMES["youtube"]
    document = _t(["HOST", "UNKNOWN"])
    m = sm.build_map(SOURCE, document, [], candidates=[], scheme=scheme)
    assert list(m["roles"]) == ["HOST"] and m["video_id"] == "src-1"
    assert sm.apply_map(document, m, scheme=scheme)["segments"][1]["speaker"] == ""
    assert sm.labels_present(_t(["GUEST_2", "HOST", "ODD"]), scheme=scheme) == ["HOST", "GUEST_2"]


def test_candidates_are_deduplicated_in_order(scheme):
    m = sm.build_map(SOURCE, _t([]), [], candidates=["b", "a", "b"], scheme=scheme)
    assert m["candidates"] == ["b", "a"]


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
