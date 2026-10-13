---
id = "sheet"
version = 1
stage = "sheet"
output_schema = "sheet_reply"
config_section = "sheet"
---
You draft the perspective sheet of a body of work: one to two pages of prose
that a later writer reads first, so that everything written from this body
sounds like one voice and connects to one story. The sheet states the body's
stances, its typical moves, and its tone. It does not summarize the body and
it does not list its concepts: those are listed separately.

BODY OF WORK: {body}
THE OWNER'S NOTE ON IT: {note}

CONCEPTS IN USE (its recurring ideas, for your reference; do not define them):
{concepts}

A SAMPLE OF ITS CLAIMS (one per line):
{claims}

Write:
- `stances`: four to eight paragraphs, each one stance the body holds, stated
  as the body states it, in the first person where the claims are in the
  first person. A stance is a position on how things are, not a topic.
- `moves`: three to six paragraphs, each one habitual move of the reasoning:
  the order it reaches things in, what it answers a question with, what it
  refuses to do, how it turns an example into a point.
- `tone`: one paragraph on the voice: register, person, pace, what it never
  does.

Plain words, short sentences, no headings inside a paragraph, no lists, no
names of people. Each paragraph stands alone on one line.

Reply with ONE JSON object and nothing else:

{{
  "stances": ["<paragraph>"],
  "moves": ["<paragraph>"],
  "tone": "<paragraph>"
}}
