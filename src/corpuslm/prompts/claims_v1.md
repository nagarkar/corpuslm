---
id = "claims"
version = 1
stage = "claims"
output_schema = "claims_reply"
config_section = "claims"
---
You read one section of {source_label} and list what it claims. Your list is
the only thing a later reader will see of this section, so be exhaustive:
every distinct assertion, definition, example, story, question, prediction
and disagreement, including ones that seem minor. Selection happens later and
is not your job.

TITLE: {title}
DATE: {date}

SECTION {section} (one line per segment, as `[index] speaker: text`; the
speaker may be empty when unknown):
{transcript}

For each claim:
- `statement`: one plain sentence of at most 25 words that stands alone, in
  your own words, faithful to what was said. It is a restatement, never a
  quotation, and it need not follow sentence boundaries: a long sentence may
  yield two claims, and a claim may draw on several lines. No "the speaker
  says"; state the claim itself.
- `speaker`: the name exactly as it appears before the colon on the lines the
  claim comes from, or "" when those lines have no speaker.
- `segments`: the indexes of the lines where the claim is made, at most six,
  the clearest first. Use only indexes that appear above.
- `kind`: "assertion" (something a reader could agree or disagree with),
  "definition" (says what a term means), "example" (a concrete case given to
  illustrate a point), "story" (something that happened, told to make a
  point), "question" (a question raised and left open), "prediction" (about
  the future), or "disagreement" (rejects or disputes what someone else
  holds; say whom in the statement).

Work through the section in order, from its first line to its last. Each
example, story, number, piece of advice, cause-and-effect step and objection
is its own claim. Do not merge two different claims into one statement. Do
not repeat a claim that is restated; cite the clearest place. A section may
begin or end mid-thought; state only what its own lines support. Skip
greetings, sponsor reads and housekeeping.

Reply with ONE JSON object and nothing else:

{{
  "claims": [
    {{"statement": "<sentence>", "speaker": "<name or empty>",
      "segments": [<index>], "kind": "<kind>"}}
  ]
}}
