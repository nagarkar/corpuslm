---
id = "monologue"
version = 1
stage = "monologue"
output_schema = "monologue_reply"
config_section = "monologue"
---
You write a monologue script of about {minutes} minutes of speech (roughly
{words} words), to be read from a teleprompter by the person whose moments
these are. It is their voice: first person, plain words, spoken rhythm. The
monologue is made from the moments below, retold generically (a person is "my
son" or "a friend", never a name), and it stays inside the perspective of the
body of work whose sheet comes first, using its stances, its moves and its
vocabulary where they fit, so that this monologue connects to the others
written from it.

{sheets}

THE BRIEF (what the owner said it is about; may be empty):
{brief}

THE MOMENTS (each with its insight, its concepts, the claims made in it and
an excerpt of what was said):
{moments}

REFERENCES (claims from sources attached to this monologue, to draw on where
they help; never to summarize):
{references}

{prior}

Write the script as paragraphs that stand alone on one line each, no headings,
no stage directions, no lists. Open inside a moment, not with a thesis. End on
the insight, said once, plainly. Propose a short working title.

Reply with ONE JSON object and nothing else:

{{
  "title": "<working title>",
  "paragraphs": ["<paragraph>"]
}}
