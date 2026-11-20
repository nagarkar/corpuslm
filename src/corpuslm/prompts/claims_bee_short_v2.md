---
id = "claims_bee_short"
version = 2
stage = "claims"
output_schema = "claims_reply"
config_section = "claims"
---
You read one section of {source_label}, recorded by a wearable, and list what
it claims and what the owner asked for or said ought to be done. Your list
is the only thing a later reader will see of this section, so be exhaustive:
every distinct assertion, definition, example, story, question, prediction and disagreement, including
ones that seem minor, and every directive. Selection happens later and is not
your job.

TITLE: {title}
DATE: {date}
OWNER: {owner} (the person wearing the recorder; their lines carry this name)
FORM: {form} (monologue: the owner alone; dialogue or group: the owner in
company; media: a podcast, video, class or gathering the recorder heard)

The recorder often fails to recognise the owner's own voice and labels it
"Unknown" or leaves the speaker empty. In a monologue, dialogue or group read
such lines as the owner's, unless the words show that someone else is
speaking. In media they are not the owner's.

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

A DIRECTIVE is something the owner said that calls for work: an instruction,
a piece of guidance, or a commitment. Only the owner's lines yield directives.
There are four kinds, and each directive names its own as `basis`:
- "wake_word": the utterance addresses "Computer" (any casing, with or
  without a comma: "Computer, remind me", "computer set a timer"), whatever
  it asks.
- "guidance": the owner says, in general terms, that work ought to be done,
  without ordering anyone: "this is a poem that we should further explore
  and write down", "we must put that into the sermons", "I think we should
  really explain what the point means", "we need a universal argument for
  it", "the next step is to draft the outline", "there should be a chapter
  on that", "we have to fix the intro", "let's add an example", "I want this
  in the book". Read by meaning, not by these phrases: any line that says
  something should be made, written, explored, explained, added, changed or
  looked into counts. The owner's "we" is the owner and their assistant. A
  general opinion about how people or the world should be ("we should all
  be kinder") is not guidance; file it under its ordinary kind.
- "instruction": a plain imperative with no wake word: "write that down",
  "look up the verse", "remember to buy milk".
- "commitment": a first-person promise about what the owner will do: "I will
  call her tomorrow".

Which of them to list depends on FORM:
- monologue: list all four.
- dialogue or group: list "wake_word" and "guidance" only. An instruction or
  a promise said to another person in the room is not a directive; file it
  under its ordinary kind instead.
- media, or any other value: list "wake_word" only.

For each directive, besides `statement` (what is to be done, restated as one
clear sentence, with what, whom and when as far as the lines say; for
guidance name the thing it is about, taken from the lines around it, so the
sentence stands alone), `speaker`, `segments` (only the line or two that
make the request) and `kind` "directive", also give (and do not quote the
lines anywhere: the words as heard are taken from the lines you cite):
- `basis`: one of the four above. When two fit, "wake_word" wins, then
  "guidance", then "instruction".
- `else_kind`: the ordinary kind this would be if it were not a directive
  (usually "assertion"; "prediction" for an intention about the future).

Work through the section in order, from its first line to its last. Each
example, story, number, piece of advice, cause-and-effect step and objection
is its own claim; each request and each piece of guidance is its own
directive. Do not merge two different claims into one statement. Do not
repeat a claim that is restated; cite the clearest place. A section may begin or end mid-thought; state only
what its own lines support. Skip greetings and small talk.

Reply with ONE JSON object and nothing else:

{{
  "claims": [
    {{"statement": "<sentence>", "speaker": "<name or empty>",
      "segments": [<index>], "kind": "<kind>"}},
    {{"statement": "<the request>", "speaker": "{owner}",
      "segments": [<index>], "kind": "directive",
      "basis": "wake_word" | "guidance" | "instruction" | "commitment",
      "else_kind": "<ordinary kind>"}}
  ]
}}
