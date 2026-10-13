---
id = "concepts"
version = 1
stage = "concepts"
output_schema = "concepts_reply"
config_section = "concepts"
---
You read a batch of claims from {source_label} and name the concepts each claim
uses. A concept is a recurring idea or phrase a body of work keeps returning to:
a named notion, a term of art, an idiom with a fixed meaning, a principle. It is
not a topic label for the whole batch, not a proper name of a person, and not a
word that merely appears once in passing.

CONCEPTS ALREADY IN USE (reuse these labels exactly, spelled as shown, whenever a
claim uses that idea, even if the claim words it differently; add a new label
only for an idea none of them covers):
{vocabulary}

CLAIMS (one per line, as `[index] text`):
{claims}

For each claim, list the labels of the concepts it uses: usually one to three,
sometimes none. A label is a short noun phrase, lowercase, singular, two to four
words at most, in the body of work's own wording where it has one. Never invent
a label for a claim that uses no recurring idea; an empty list is a fine answer.
Keep the index exactly as given and answer for every claim in the batch.

Reply with ONE JSON object and nothing else:

{{
  "claims": [
    {{"index": <index>, "concepts": ["<label>", "<label>"]}}
  ]
}}
