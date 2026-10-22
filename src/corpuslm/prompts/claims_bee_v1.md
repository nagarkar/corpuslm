---
id = "claims_bee"
version = 1
stage = "claims"
output_schema = "claims_reply"
config_section = "claims"
---
You read one section of {source_label}, recorded by a wearable, and list what
it claims and what the owner asked for. Your list is the only thing a later
reader will see of this section, so be exhaustive: every distinct assertion,
definition, example, story, question, prediction and disagreement, including
ones that seem minor, and every directive. Selection happens later and is not
your job.

TITLE: {title}
DATE: {date}
OWNER: {owner} (the person wearing the recorder; their lines carry this name)
FORM: {form} (monologue: the owner alone; dialogue, group or media: company)

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
  the future), "disagreement" (rejects or disputes what someone else holds;
  say whom in the statement), or "directive" (below).

A DIRECTIVE is an instruction or a commitment the owner spoke: something to
do, remember, buy, send, book, look up, or a first-person promise ("I will
call her tomorrow"). Only the owner's lines yield directives. Which lines are
candidates depends on FORM:
- monologue: every imperative or first-person commitment the owner speaks is
  a candidate, whether or not it names "Computer".
- dialogue, group or media: ONLY an utterance that addresses "Computer" is a
  directive. An imperative said to another person in the room is not; file
  it under its ordinary kind instead.

For each directive, besides `statement` (the request restated as one clear
sentence, with what, whom and when as far as the lines say), `speaker`,
`segments` and `kind` "directive", also give:
- `addressee`: "computer" when the utterance addresses "Computer" (any
  casing, with or without a comma: "Computer, remind me", "computer set a
  timer"), whatever it asks; otherwise "self" (a note to the owner). A
  household member is never an addressee.
- `heard`: the words as heard, verbatim from the cited lines, the whole
  utterance from the wake word or the imperative to the end of the request.
- `else_kind`: the ordinary kind this would be if it were not a directive
  (usually "assertion"; "prediction" for an intention about the future).

Work through the section in order, from its first line to its last. Each
example, story, number, piece of advice, cause-and-effect step and objection
is its own claim; each request is its own directive. Do not merge two
different claims into one statement. Do not repeat a claim that is restated;
cite the clearest place. A section may begin or end mid-thought; state only
what its own lines support. Skip greetings and small talk.

Reply with ONE JSON object and nothing else:

{{
  "claims": [
    {{"statement": "<sentence>", "speaker": "<name or empty>",
      "segments": [<index>], "kind": "<kind>"}},
    {{"statement": "<the request>", "speaker": "{owner}",
      "segments": [<index>], "kind": "directive",
      "addressee": "computer" | "self", "heard": "<the words as heard>",
      "else_kind": "<ordinary kind>"}}
  ]
}}
