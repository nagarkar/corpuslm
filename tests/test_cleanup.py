"""Protected vocabulary and spell fix (`src/corpuslm/cleanup.py`): deterministic, per segment,
never over the flattened text; protected spans are law for every later step. Sanitized
synthetic text only."""

import copy

from corpuslm import cleanup, transcript


def _t(*texts, language="en"):
    return transcript.from_segments(
        "any-source",
        [{"t0": float(i), "t1": float(i + 1), "text": s} for i, s in enumerate(texts)],
        {"language": language, "generated": None, "fetcher": "test"},
        "test.transcript/1",
    )


def test_texts_become_the_protected_vocabulary():
    vocab = cleanup.vocabulary_from_texts(
        names=[("source:channel", "Quillfeather Society of Lisbon")],
        texts=[
            ("source:title", "Law of Karma in Advaita Vedanta | Dr Quillfeather Brightwater"),
            ("source:description", "A talk by Dr Quillfeather Brightwater at the Society."),
        ],
        tags=[
            ("source:tag", "vedanta"),
            ("source:tag", "Advaita Vedanta"),
            ("source:tag", "karma"),
        ],
    )
    assert "Quillfeather Society of Lisbon" in vocab.terms
    assert "Dr Quillfeather Brightwater" in vocab.terms
    assert "Advaita Vedanta" in vocab.terms
    assert "karma" not in vocab.terms  # lowercase tags are not names
    assert vocab.sources["Quillfeather Society of Lisbon"] == "source:channel"
    assert vocab.sources["Dr Quillfeather Brightwater"] == "source:title"  # first label wins


def test_empty_and_short_words_give_no_terms():
    vocab = cleanup.vocabulary_from_texts(names=[("a", None), ("a", "Ann")], texts=[("b", None)])
    assert vocab.terms == [] and vocab.substitutions == {}


def test_protect_vocabulary_restores_protected_spellings_and_marks_them():
    vocab = cleanup.Vocabulary(
        terms=["Dr Quillfeather", "Advaita"],
        substitutions={"quill fe ather": "Quillfeather"},
        sources={},
    )
    t, report = cleanup.protect_vocabulary(
        _t(
            "today dr quillfeather explains advaita",
            "dr quill fe ather again",
            "nothing protected here",
        ),
        vocab,
    )
    assert t["state"] == "DETERMINISTIC-ONLY"
    assert t["segments"][0]["text"] == "today Dr Quillfeather explains Advaita"
    assert t["segments"][0]["protected"] == ["Dr Quillfeather", "Advaita"]
    assert t["segments"][0]["raw"] == "today dr quillfeather explains advaita"
    assert t["segments"][1]["text"] == "Dr Quillfeather again"
    assert "raw" not in t["segments"][2] and "protected" not in t["segments"][2]
    assert report["segments_changed"] == 2 and report["terms"] == 2
    # the split-token rule also matched the joined form in segment 0 (a case fix)
    assert t["passes"]["pass1"]["substitutions_applied"] == 2


def test_protect_vocabulary_joins_a_split_protected_term():
    vocab = cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    t, _ = cleanup.protect_vocabulary(_t("as quill feather said"), vocab)
    assert t["segments"][0]["text"] == "as Quillfeather said"


def test_spell_fix_fixes_lowercase_typos_and_leaves_protected_and_names_alone():
    vocab = cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    t, _ = cleanup.protect_vocabulary(
        _t(
            "the elephnts were realy big",
            "Quillfeather spoke",
            "Bombay is a city and 42 is a number",
        ),
        vocab,
    )
    t, report = cleanup.spell_fix(t, language="en")
    # "elephnts" has one dictionary neighbour; "realy" has three (real, really, relay)
    # and is left for a model step, which has the sentence.
    assert t["segments"][0]["text"] == "the elephants were realy big"
    assert t["segments"][1]["text"] == "Quillfeather spoke"
    assert t["segments"][2]["text"] == "Bombay is a city and 42 is a number"
    assert report["status"] == "ran" and report["tokens_changed"] == 1


def test_spell_fix_is_skipped_for_a_non_english_source_with_provenance():
    t, report = cleanup.spell_fix(_t("namaste sabko", language="hi"), language="hi")
    assert report["status"] == "skipped" and "hi" in report["reason"]
    assert t["passes"]["pass2"]["status"] == "skipped"
    assert t["state"] == "DETERMINISTIC-ONLY"


def test_a_rerun_changes_nothing():
    vocab = cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    t, _ = cleanup.protect_vocabulary(_t("quillfeather and the elephnts"), vocab)
    t, _ = cleanup.spell_fix(t, language="en")
    again = copy.deepcopy(t)
    again, _ = cleanup.protect_vocabulary(again, vocab)
    again, _ = cleanup.spell_fix(again, language="en")
    assert [s["text"] for s in again["segments"]] == [s["text"] for s in t["segments"]]
    assert [s.get("raw") for s in again["segments"]] == [s.get("raw") for s in t["segments"]]


def test_correction_memory_substitutes_verdicts_after_a_model_step_and_protects_them():
    vocab = cleanup.Vocabulary(
        terms=["Quillfeather"], substitutions={"quill fether": "Quillfeather"}, sources={}
    )
    t, report = cleanup.apply_correction_memory(_t("so quill fether said", "no match"), vocab)
    assert t["segments"][0]["text"] == "so Quillfeather said"
    assert t["segments"][0]["protected"] == ["Quillfeather"]
    assert report == {"applied": 1, "segments_changed": 1}
    assert t["passes"]["memory"]["rules"] == 1


def test_is_dictionary_word_and_the_fingerprint():
    assert cleanup.is_dictionary_word("elephants") and not cleanup.is_dictionary_word("elephnts")
    vocab = cleanup.Vocabulary(terms=["B", "A"], substitutions={"y": "Y", "x": "X"})
    assert cleanup.vocabulary_fingerprint(vocab) == (
        '{"substitutions": {"x": "X", "y": "Y"}, "terms": ["A", "B"]}'
    )
    other = cleanup.Vocabulary(terms=["C", "A"], substitutions={"z": "Z"}, sources={"C": "s"})
    merged = vocab.merged(other)
    assert merged.terms == ["B", "A", "C"] and merged.substitutions["z"] == "Z"
