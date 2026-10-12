"""Protected vocabulary and spell fix (`src/corpuslm/basic_cleanup.py`): deterministic, per segment,
never over the flattened text; protected spans are law for every later step. Sanitized
synthetic text only."""

import copy

from corpuslm import basic_cleanup, transcript


def _texts_document(*texts, language="en"):
    return transcript.from_segments(
        "any-source",
        [
            {"t0": float(segment_index), "t1": float(segment_index + 1), "text": text}
            for segment_index, text in enumerate(texts)
        ],
        {"language": language, "generated": None, "fetcher": "test"},
        "test.transcript/1",
    )


def test_texts_become_the_protected_vocabulary():
    vocab = basic_cleanup.vocabulary_from_texts(
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
    vocab = basic_cleanup.vocabulary_from_texts(
        names=[("a", None), ("a", "Ann")], texts=[("b", None)]
    )
    assert vocab.terms == [] and vocab.substitutions == {}


def test_protect_vocabulary_restores_protected_spellings_and_marks_them():
    vocab = basic_cleanup.Vocabulary(
        terms=["Dr Quillfeather", "Advaita"],
        substitutions={"quill fe ather": "Quillfeather"},
        sources={},
    )
    document, report = basic_cleanup.protect_vocabulary(
        _texts_document(
            "today dr quillfeather explains advaita",
            "dr quill fe ather again",
            "nothing protected here",
        ),
        vocab,
    )
    assert document["state"] == "DETERMINISTIC-ONLY"
    assert document["segments"][0]["text"] == "today Dr Quillfeather explains Advaita"
    assert document["segments"][0]["protected"] == ["Dr Quillfeather", "Advaita"]
    assert document["segments"][0]["raw"] == "today dr quillfeather explains advaita"
    assert document["segments"][1]["text"] == "Dr Quillfeather again"
    assert "raw" not in document["segments"][2] and "protected" not in document["segments"][2]
    assert report["segments_changed"] == 2 and report["terms"] == 2
    # the split-token rule also matched the joined form in segment 0 (a case fix)
    assert document["passes"]["pass1"]["substitutions_applied"] == 2


def test_protect_vocabulary_joins_a_split_protected_term():
    vocab = basic_cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    document, _ = basic_cleanup.protect_vocabulary(_texts_document("as quill feather said"), vocab)
    assert document["segments"][0]["text"] == "as Quillfeather said"


def test_spell_fix_fixes_lowercase_typos_and_leaves_protected_and_names_alone():
    vocab = basic_cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    document, _ = basic_cleanup.protect_vocabulary(
        _texts_document(
            "the elephnts were realy big",
            "Quillfeather spoke",
            "Bombay is a city and 42 is a number",
        ),
        vocab,
    )
    document, report = basic_cleanup.spell_fix(document, language="en")
    # "elephnts" has one dictionary neighbour; "realy" has three (real, really, relay)
    # and is left for a model step, which has the sentence.
    assert document["segments"][0]["text"] == "the elephants were realy big"
    assert document["segments"][1]["text"] == "Quillfeather spoke"
    assert document["segments"][2]["text"] == "Bombay is a city and 42 is a number"
    assert report["status"] == "ran" and report["tokens_changed"] == 1


def test_spell_fix_is_skipped_for_a_non_english_source_with_provenance():
    document, report = basic_cleanup.spell_fix(
        _texts_document("namaste sabko", language="hi"), language="hi"
    )
    assert report["status"] == "skipped" and "hi" in report["reason"]
    assert document["passes"]["pass2"]["status"] == "skipped"
    assert document["state"] == "DETERMINISTIC-ONLY"


def test_a_rerun_changes_nothing():
    vocab = basic_cleanup.Vocabulary(terms=["Quillfeather"], substitutions={}, sources={})
    document, _ = basic_cleanup.protect_vocabulary(
        _texts_document("quillfeather and the elephnts"), vocab
    )
    document, _ = basic_cleanup.spell_fix(document, language="en")
    again = copy.deepcopy(document)
    again, _ = basic_cleanup.protect_vocabulary(again, vocab)
    again, _ = basic_cleanup.spell_fix(again, language="en")
    assert [segment["text"] for segment in again["segments"]] == [
        segment["text"] for segment in document["segments"]
    ]
    assert [segment.get("raw") for segment in again["segments"]] == [
        segment.get("raw") for segment in document["segments"]
    ]


def test_correction_memory_substitutes_verdicts_after_a_model_step_and_protects_them():
    vocab = basic_cleanup.Vocabulary(
        terms=["Quillfeather"], substitutions={"quill fether": "Quillfeather"}, sources={}
    )
    document, report = basic_cleanup.apply_correction_memory(
        _texts_document("so quill fether said", "no match"), vocab
    )
    assert document["segments"][0]["text"] == "so Quillfeather said"
    assert document["segments"][0]["protected"] == ["Quillfeather"]
    assert report == {"applied": 1, "segments_changed": 1}
    assert document["passes"]["memory"]["rules"] == 1


def test_is_dictionary_word_and_the_fingerprint():
    assert basic_cleanup.is_dictionary_word("elephants") and not basic_cleanup.is_dictionary_word(
        "elephnts"
    )
    vocab = basic_cleanup.Vocabulary(terms=["B", "A"], substitutions={"y": "Y", "x": "X"})
    assert basic_cleanup.vocabulary_fingerprint(vocab) == (
        '{"substitutions": {"x": "X", "y": "Y"}, "terms": ["A", "B"]}'
    )
    other = basic_cleanup.Vocabulary(terms=["C", "A"], substitutions={"z": "Z"}, sources={"C": "s"})
    merged = vocab.merged(other)
    assert merged.terms == ["B", "A", "C"] and merged.substitutions["z"] == "Z"
