---
id = "cleanup_bee"
version = 1
stage = "model_cleanup"
output_schema = "model_cleanup_bee_reply"
config_section = "cleanup"
---
You clean one batch of {source_label}, a transcript of speech from a wearable
recorder that earlier passes have already corrected mechanically. Segment
boundaries and timestamps are fixed; you edit only the text inside a segment.

The speaker label at the start of each line comes from the recorder and is
fixed. You never assign, change, merge or invent a speaker label. You may
only propose what a label is called, from what the dialogue says.

PROTECTED VOCABULARY (law: never alter these spellings; a mis-hearing of one
of them is corrected TO the protected form):
{protected}

SPEAKERS SO FAR (labels used in earlier batches, with any name already
bound to them):
{roles}

BATCH (segments as `[index] label: text`; lines marked `(context)` are
read-only and must not appear in your edits):
{batch}

Reply with ONE JSON object and nothing else:

{{
  "edits": {{"<index>": "<new text>"}},
  "ambiguities": [
    {{"idx": <index>, "span": "<the words as heard>",
      "candidates": ["<the words as heard>", "<reading 2>", ...],
      "probabilities": [<0-1>, <0-1>, ...], "pick": "<your reading>"}}
  ],
  "bindings": [
    {{"role": "<a speaker label exactly as shown in the batch>", "name": "<name>", "idx": <index>,
      "anchor": "<verbatim words from that segment that support the binding>",
      "confidence": <0-1>}}
  ]
}}

Rules:
- `edits` lists ONLY segments whose text you changed: punctuation, sentence
  case, obvious mis-hearings that earlier passes missed, dropped words that
  the audio plainly implies. Never paraphrase, never remove content, never
  merge or split segments, never edit a `(context)` line, never edit the
  speaker label.
- Do not send `roles`. Speaker labels are not yours to give.
- `ambiguities` are CONSEQUENTIAL only: a name, a technical term, or a reading
  that changes the meaning of the sentence. Filler, punctuation and casing
  are never ambiguities. Apply your pick in `edits`.
- An ambiguity's `candidates` are every reading you weighed, and always
  include the words as heard. `probabilities` gives one number per
  candidate, in the same order: your probability that THAT candidate is
  what the speaker actually said. They sum to 1. A probability of 0.9
  means you expect to be wrong about 1 time in 10; do not round toward
  certainty. `pick` is the candidate with the highest probability.
- `bindings` name a speaker label only where THIS batch's dialogue supports
  it (an introduction, a self-reference, being addressed by name), with the
  verbatim anchor. Never bind a name to a label you are guessing at.
- Indices outside this batch are an error. Never invent; if nothing needs
  changing, reply with empty `edits`, `ambiguities` and `bindings`.
