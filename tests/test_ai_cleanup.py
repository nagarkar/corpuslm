"""AI cleanup (`src/corpuslm/ai_cleanup.py`): batches with read-only overlap, strict
reply bounds, edits-only replies, ambiguities with probabilities, bindings with anchors, and
fixed labels no reply may change. Run over a YouTube-shaped scheme and Bee's. Synthetic text
only, no model."""

import pytest
from schemes import LABELS, SCHEMES

from corpuslm import ai_cleanup as mc
from corpuslm import prompts, schemas
from corpuslm.speaker_map import bee_scheme


@pytest.fixture(params=["youtube", "bee"])
def kind(request):
    return request.param


@pytest.fixture
def scheme(kind):
    return SCHEMES[kind]


def _t(number, kind="youtube", words=8, labelled=None):
    """`n` segments. Fixed-label sources carry their labels from the start; the others get
    theirs from the model, so they start without."""
    labelled = SCHEMES[kind].fixed_labels if labelled is None else labelled
    a, b = LABELS[kind]
    segments = []
    for i in range(number):
        segment = {
            "idx": i,
            "t0": float(i),
            "t1": float(i + 1),
            "speaker": "",
            "text": " ".join(f"w{i}x{k}" for k in range(words)),
        }
        if labelled:
            segment["role"] = a if i % 2 else b
        segments.append(segment)
    return {"video_id": "src-1", "segments": segments, "words": []}


def _reply(**parts):
    return {"edits": {}, "roles": {}, "ambiguities": [], "bindings": []} | parts


def _first(document, scheme):
    return mc.batches(document, batch_tokens=10_000, overlap_segments=0)[0]


def test_batches_cut_on_segment_boundaries_with_read_only_overlap(kind, scheme):
    document = _t(100, kind)
    batches = mc.batches(document, batch_tokens=100, overlap_segments=3)
    assert len(batches) > 5
    assert batches[0].context == (0, 0) and batches[0].start == 0
    for prev, cur in zip(batches, batches[1:], strict=False):
        assert cur.start == prev.end  # no gaps, no double edits
        assert cur.context == (max(0, prev.end - 3), prev.end)
    assert batches[-1].end == 100
    rendered = mc.render(document, batches[1], scheme=scheme)
    assert "(context)" in rendered and f"[{batches[1].start}]" in rendered


def test_render_shows_a_label_only_when_the_labels_are_fixed(kind, scheme):
    document = _t(4, kind, labelled=True)
    line = mc.render(document, _first(document, scheme), scheme=scheme).splitlines()[1]
    label = document["segments"][1]["role"]
    assert (f"[1] {label}: w1x0" in line) == scheme.fixed_labels
    assert line.startswith("[1] ")


def test_apply_rejects_an_index_outside_the_batch_and_protects_spans(kind, scheme):
    document = _t(10, kind)
    document["segments"][4]["protected"] = ["w4x0"]
    batch = _first(document, scheme)
    _, stray = mc.apply(document, batch, _reply(edits={"42": "x"}), next_key=1, scheme=scheme)
    assert stray["stray_edits"] == 1 and stray["edits"] == 0
    with pytest.raises(mc.BatchError, match="42"):
        mc.apply(
            document, batch, _reply(roles={"42": [LABELS[kind][0], 0.9]}), next_key=1, scheme=scheme
        )
    other = LABELS[kind][1]
    reply = _reply(
        edits={"4": "dropped the protected word", "5": "W5x0 w5x1, w5x2."},
        ambiguities=[
            {
                "idx": 5,
                "span": "w5x1",
                "candidates": ["w5x1", "wsxl"],
                "pick": "w5x1",
                "confidence": 0.5,
            }
        ],
        bindings=[{"role": other, "name": "Dr. Five", "idx": 5, "anchor": "w5x1"}],
    )
    if not scheme.fixed_labels:
        reply["roles"] = {"5": [other, 0.7]}
    document, stats = mc.apply(document, batch, reply, next_key=1, scheme=scheme)
    assert document["segments"][4]["text"].startswith("w4x0")  # protected: edit declined
    assert stats["protected_violations"] == 1 and stats["edits"] == 1
    assert document["segments"][5]["text"] == "W5x0 w5x1, w5x2." and document["segments"][5]["raw"]
    if not scheme.fixed_labels:
        assert (
            document["segments"][5]["role"] == other
            and document["segments"][5]["role_confidence"] == 0.7
        )
    assert (
        document["ambiguities"][0]["key"] == "amb-0001"
        and document["ambiguities"][0]["status"] == "pending"
    )
    assert stats["bindings"] == [
        {"role": other, "name": "Dr. Five", "idx": 5, "anchor": "w5x1", "confidence": 0.8}
    ]


def test_apply_never_changes_segment_boundaries_and_protects_spans(kind, scheme):
    document = _t(10, kind)
    before = [(s["idx"], s["t0"], s["t1"]) for s in document["segments"]]
    batch = _first(document, scheme)
    document, _ = mc.apply(
        document,
        batch,
        _reply(edits={str(i): f"Edit {i}." for i in range(10)}),
        next_key=1,
        scheme=scheme,
    )
    assert [(s["idx"], s["t0"], s["t1"]) for s in document["segments"]] == before
    assert len(document["segments"]) == 10


def test_an_ambiguity_carries_a_probability_per_candidate_and_its_confidence_is_the_picks(
    kind, scheme
):
    document = _t(10, kind)
    batch = _first(document, scheme)
    reply = _reply(
        ambiguities=[
            {
                "idx": 5,
                "span": "w5x1",
                "candidates": ["w5x1", "wsxl", "w5xl"],
                "probabilities": [0.2, 0.7, 0.08],
                "pick": "wsxl",
            }
        ]
    )
    mc.check_bounds(batch, reply, scheme=scheme, document=document)
    document, _ = mc.apply(document, batch, reply, next_key=1, scheme=scheme)
    a = document["ambiguities"][0]
    assert a["probabilities"] == [0.204, 0.714, 0.082]  # rescaled to sum to 1
    assert a["confidence"] == 0.714


def test_a_probability_reply_that_does_not_line_up_with_its_candidates_is_bad(kind, scheme):
    document = _t(10, kind)
    batch = _first(document, scheme)
    base = {"idx": 5, "span": "w5x1", "candidates": ["w5x1", "wsxl"], "pick": "wsxl"}
    for bad, why in (
        ({"probabilities": [1.0]}, "one probability per candidate"),
        ({"probabilities": [0.0, 0.0]}, "sum"),
        ({"probabilities": [0.4, 0.6], "pick": "other"}, "pick"),
    ):
        with pytest.raises(ValueError, match=why):
            mc.check_bounds(batch, _reply(ambiguities=[base | bad]), scheme=scheme)


def test_labels_and_edits_on_context_lines_are_ignored_and_counted(kind, scheme):
    document = _t(20, kind)
    batch = mc.batches(document, batch_tokens=100, overlap_segments=3)[1]
    ai_cleanup_setup = batch.context[0]
    a, b = LABELS[kind]
    # a fixed-label reply may only repeat the segment's own label
    own = {i: document["segments"][i].get("role", a) for i in (ai_cleanup_setup, batch.start)}
    roles = {
        str(ai_cleanup_setup): [own[ai_cleanup_setup], 0.9],
        str(batch.start): [own[batch.start], 0.9],
    }
    mc.check_bounds(batch, _reply(roles=roles), scheme=scheme, document=document)
    document, stats = mc.apply(document, batch, _reply(roles=roles), next_key=1, scheme=scheme)
    assert stats["context_roles_ignored"] == 1
    assert stats["roles"] == (0 if scheme.fixed_labels else 1)
    if not scheme.fixed_labels:
        assert "role" not in document["segments"][ai_cleanup_setup]
    document, stats = mc.apply(
        document, batch, _reply(edits={str(ai_cleanup_setup): "x"}), next_key=1, scheme=scheme
    )
    assert stats["stray_edits"] == 1 and document["segments"][ai_cleanup_setup]["text"] != "x"


def test_a_binding_on_a_context_line_is_kept_and_one_beyond_what_the_model_saw_fails(kind, scheme):
    document = _t(20, kind)
    batch = mc.batches(document, batch_tokens=100, overlap_segments=3)[1]
    ai_cleanup_setup = batch.context[0]
    role = document["segments"][ai_cleanup_setup].get("role", LABELS[kind][1])
    binding = {
        "role": role,
        "name": "Ann",
        "idx": ai_cleanup_setup,
        "anchor": f"w{ai_cleanup_setup}x1",
    }
    reply = _reply(bindings=[binding])
    mc.check_bounds(batch, reply, scheme=scheme)
    document, stats = mc.apply(document, batch, reply, next_key=1, scheme=scheme)
    assert stats["bindings"] == [
        {
            "role": role,
            "name": "Ann",
            "idx": ai_cleanup_setup,
            "anchor": f"w{ai_cleanup_setup}x1",
            "confidence": 0.8,
        }
    ]
    unseen = _reply(bindings=[binding | {"idx": ai_cleanup_setup - 1}])
    with pytest.raises(ValueError, match="bindings: index"):
        mc.check_bounds(batch, unseen, scheme=scheme)
    ambiguity = {
        "idx": ai_cleanup_setup,
        "span": f"w{ai_cleanup_setup}x1",
        "candidates": ["a", "b"],
        "pick": "a",
    }
    with pytest.raises(ValueError, match="ambiguities: index"):
        mc.check_bounds(batch, _reply(ambiguities=[ambiguity]), scheme=scheme)


def test_role_summary_carries_labels_forward(kind, scheme):
    document = _t(6, kind, labelled=True)
    a, b = LABELS[kind]
    summary = mc.role_summary(
        document, upto=6, bindings=[{"role": b, "name": "Ann"}], scheme=scheme
    )
    assert a in summary and b in summary and "Ann" in summary
    assert mc.role_summary(_t(6, kind, labelled=False), upto=6, bindings=[], scheme=scheme) == (
        "(none yet)"
    )


def test_role_summary_orders_labels_by_scheme_and_fixed_ones_by_first_seen(kind, scheme):
    document = _t(4, kind, labelled=True)
    document["segments"][0]["role"] = "Speaker 9"  # outside the scheme's vocabulary
    summary = mc.role_summary(document, upto=4, bindings=[], scheme=scheme)
    if scheme.fixed_labels:
        assert summary == (
            "Speaker 9: 1 segments so far; Quill: 2 segments so far; Speaker 2: 1 segments so far"
        )
    else:
        assert summary == "HOST: 2 segments so far; GUEST_1: 1 segments so far"


def test_the_unresolved_label_is_listed_in_a_summary_where_the_scheme_orders_it():
    scheme = SCHEMES["youtube"]
    document = _t(3, "youtube", labelled=False)
    for segment, role in zip(document["segments"], ["UNKNOWN", "GUEST_1", "HOST"], strict=True):
        segment["role"] = role
    summary = mc.role_summary(document, upto=3, bindings=[], scheme=scheme)
    assert (
        summary == "HOST: 1 segments so far; GUEST_1: 1 segments so far; UNKNOWN: 1 segments so far"
    )


def test_fixed_labels_reject_a_reply_that_relabels_a_speaker():
    scheme = SCHEMES["bee"]
    document = _t(10, "bee")
    batch = _first(document, scheme)
    own = document["segments"][3]["role"]
    relabel = _reply(roles={"3": ["Speaker 7", 0.9]})
    with pytest.raises(ValueError, match="relabel"):
        mc.check_bounds(batch, relabel, scheme=scheme, document=document)  # before it is cached
    with pytest.raises(mc.BatchError, match="relabel"):
        mc.apply(document, batch, relabel, next_key=1, scheme=scheme)
    assert document["segments"][3]["role"] == own  # untouched
    # repeating its own label changes nothing and is allowed
    document, stats = mc.apply(
        document, batch, _reply(roles={"3": [own, 0.9]}), next_key=1, scheme=scheme
    )
    assert document["segments"][3]["role"] == own and stats["roles"] == 0
    assert "role_confidence" not in document["segments"][3]
    # a segment that carries no label cannot be given one
    del document["segments"][3]["role"]
    with pytest.raises(mc.BatchError, match="fixed"):
        mc.apply(document, batch, _reply(roles={"3": [own, 0.9]}), next_key=1, scheme=scheme)


def test_a_scheme_that_is_not_fixed_lets_the_reply_relabel_and_declines_unknown_labels():
    scheme = SCHEMES["youtube"]
    document = _t(10, "youtube", labelled=False)
    batch = _first(document, scheme)
    document, stats = mc.apply(
        document, batch, _reply(roles={"3": ["GUEST_2", 0.9]}), next_key=1, scheme=scheme
    )
    document, stats = mc.apply(
        document, batch, _reply(roles={"3": ["HOST", 0.8]}), next_key=1, scheme=scheme
    )
    assert (
        document["segments"][3]["role"] == "HOST"
        and document["segments"][3]["speaker"] == "GUEST_2"
    )
    with pytest.raises(mc.BatchError, match="not a role label"):
        mc.apply(
            document, batch, _reply(roles={"3": ["Speaker 7", 0.9]}), next_key=1, scheme=scheme
        )
    document, _ = mc.apply(
        document, batch, _reply(roles={"4": ["UNKNOWN", 0.4]}), next_key=1, scheme=scheme
    )
    assert document["segments"][4]["speaker"] == ""


def test_a_fixed_label_binding_for_a_label_no_segment_carries_is_dropped():
    scheme = SCHEMES["bee"]
    document = _t(10, "bee")
    batch = _first(document, scheme)
    binding = {"role": "Speaker 42", "name": "Ann", "idx": 1, "anchor": "w1x1"}
    _, stats = mc.apply(document, batch, _reply(bindings=[binding]), next_key=1, scheme=scheme)
    assert stats["bindings"] == [] and stats["dropped_bindings"] == 1


def test_a_binding_without_its_anchor_is_dropped_and_counted(kind, scheme):
    document = _t(10, kind, labelled=True)
    batch = _first(document, scheme)
    role = document["segments"][1]["role"]
    binding = {"role": role, "name": "Ann", "idx": 1, "anchor": "nowhere"}
    _, stats = mc.apply(document, batch, _reply(bindings=[binding]), next_key=1, scheme=scheme)
    assert stats["dropped_bindings"] == 1


def test_an_ambiguity_with_no_anchor_in_its_segment_is_dropped_and_counted(kind, scheme):
    document = _t(10, kind)
    batch = _first(document, scheme)
    amb = {"idx": 2, "span": "nowhere", "candidates": ["nowhere"], "pick": "nowhere"}
    document, stats = mc.apply(
        document, batch, _reply(ambiguities=[amb]), next_key=1, scheme=scheme
    )
    assert stats["dropped_ambiguities"] == 1 and document["ambiguities"] == []


def test_drop_unanchored_removes_items_whose_words_left_their_segment():
    document = _t(3)
    document["ambiguities"] = [
        {"idx": 0, "span": "w0x1", "pick": "w0x1", "candidates": ["w0x1"]},
        {"idx": 1, "span": "gone", "pick": "gone", "candidates": ["gone"]},
    ]
    assert mc.drop_unanchored(document) == 1 and len(document["ambiguities"]) == 1


def test_cap_ambiguities_keeps_the_least_confident_pending():
    document = {
        "ambiguities": [
            {"key": f"amb-000{i}", "status": "pending", "confidence": c}
            for i, c in enumerate([0.9, 0.3, 0.7, 0.1, 0.5])
        ]
    }
    assert mc.cap_ambiguities(document, max_new=2) == 3
    kept = [a["confidence"] for a in document["ambiguities"] if a["status"] == "pending"]
    assert sorted(kept) == [0.1, 0.3]
    assert mc.cap_ambiguities(document, max_new=0) == 0


def test_the_bee_prompt_is_shipped_and_takes_exactly_its_four_variables():
    prompt = prompts.load(bee_scheme("Quill").prompt_id)
    assert prompt.path.name == "cleanup_bee_v1.md" and prompt.path.parent == (
        prompts.BUILT_IN_DIRECTORY
    )
    assert prompt.id == "cleanup_bee" and prompt.version == 1
    assert prompt.output_schema == "model_cleanup_bee_reply"
    body = prompt.body
    assert set(prompt.placeholders()) == {"source_label", "protected", "roles", "batch"}
    for banned in ("HOST", "GUEST"):
        assert banned not in body  # no assumption about who asks and who answers
    prompt.render({"source_label": "a", "protected": "b", "roles": "c", "batch": "d"})


def test_the_bee_reply_schema_accepts_a_reply_without_roles_and_names_bad_properties():
    reply = {
        "edits": {"1": "Hello."},
        "ambiguities": [
            {
                "idx": 1,
                "span": "a",
                "candidates": ["a", "b"],
                "pick": "a",
                "probabilities": [0.6, 0.4],
            }
        ],
        "bindings": [{"role": "Speaker 2", "name": "Ann", "idx": 1, "anchor": "a"}],
    }
    schemas.validate_against(
        "model_cleanup_bee_reply", schemas.schema("model_cleanup_bee_reply"), reply
    )
    schemas.validate_against(
        "model_cleanup_bee_reply",
        schemas.schema("model_cleanup_bee_reply"),
        reply | {"roles": {"1": ["Speaker 2", 0.9]}},
    )
    with pytest.raises(schemas.SchemaError, match="extra"):
        schemas.validate_against(
            "model_cleanup_bee_reply",
            schemas.schema("model_cleanup_bee_reply"),
            reply | {"extra": 1},
        )
    required = schemas.schema("model_cleanup_bee_reply")["required"]
    assert required == ["edits", "ambiguities", "bindings"]


def test_fixed_labels_are_adopted_from_the_speakers():
    document = {
        "segments": [
            {"idx": 0, "speaker": "Quill", "text": "a"},
            {"idx": 1, "speaker": "", "text": "b"},
            {"idx": 2, "speaker": "Speaker 2", "role": "Speaker 2", "text": "c"},
        ]
    }
    mc.adopt_fixed_labels(document)
    assert [segment.get("role") for segment in document["segments"]] == ["Quill", None, "Speaker 2"]
