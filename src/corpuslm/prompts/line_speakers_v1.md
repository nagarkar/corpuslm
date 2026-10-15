---
id = "line_speakers"
version = 1
stage = "line_speakers"
output_schema = "line_speakers_reply"
config_section = "line_speakers"
---
You read a transcript from a wearable recorder that {owner} wears all day. The
recorder recognizes only {owner}'s voice. Every line it labels "{owner}" is
{owner}. Every other line is labeled "Unknown", and Unknown is a mix of
{owner} himself and everyone and everything else.

THE HOUSEHOLD (the people who usually speak, with their roles; the recorder's
summary and the recognizer may misspell their names):
{household}

Who an Unknown line usually is:
- {owner} himself, when the recorder failed to match his voice (he turned away,
  spoke quietly, there was noise). This is very common: an Unknown line that
  carries on {owner}'s sentence, argument, explanation or story, with little
  or no pause, is {owner}.
- {owner} talking to a voice assistant or dictating a message (to an AI, a
  phone, a computer): commands, dictated sentences, "send", "stop", spelled
  names. These are {owner}, never media and never another person.
- a household member, who typically answers, agrees, objects, asks or reacts
  to what was just said. When the summary or the lines show who was present,
  pick that person; prefer a household member over an unnamed adult.
- someone outside the household (a friend, colleague, shopkeeper): "other".
- media playing nearby (a video, podcast, TV, a voice assistant), which sounds
  scripted or broadcast, is often on an unrelated topic, and does not respond
  to the people in the room. Sometimes {owner} plays media on purpose to make a
  point and then talks about it; it is still media.
- noise the recognizer turned into words.

Two facts to weigh before calling anything media or another person:
- Roughly half of all Unknown lines are {owner}. When unsure between {owner}
  and someone else, look at whether the line carries his train of thought.
- The recorder never labels media as "{owner}". If some lines of a thread or
  topic are labeled "{owner}", that thread is {owner} speaking live, and its
  Unknown lines are {owner} too, even when it sounds like a lecture, a reading
  or a monologue, and even when two topics interleave.

The recorder's own summary of the conversation (it may be wrong in places; use
it for who was present and whether media was playing):
{summary}

LINES (`[index] start-end seconds | label: text`; a line may hold two
speakers when there was no pause between them):
{transcript}

Lines before [{first}] are context only. Answer for every line labeled Unknown
from [{first}] to [{last}], and for no other line.

First decide who was present: read the summary and the lines for who is
named, addressed or described ("my son", "Mom", "Baba", a school, a job).
A role word is evidence for the household member in that role: "son", "his
boy", "the kids" or a child's voice points to the son; "daughter" to the
daughter; "wife", "spouse" or "Mom" to the wife. List only people with evidence; a household
member with no evidence was not there. Use "other" only for someone who is
clearly not in the household. Then, for each of those lines, decide who spoke it, choosing
only among {owner} and the people you listed:
- `who`: a household member's name exactly as listed above (use {owner} for
  {owner}), or "other", "media", "noise", "unclear". Never any other name.
- `confidence`: 0 to 1.
A line is another person only when it reads as a reply, reaction or question to
what was just said, or when its voice clearly differs (a child's phrasing, a
different topic). Judge each line by what it says in context: does it continue the previous
speaker, answer them, or ignore them? Do not decide by the label alone, and do
not assume someone else spoke just because the line is Unknown.

Reply with JSON only:
{{"present": [{{"name": "<household name or other>", "evidence": "<a few words>"}}], "lines": [{{"index": <index>, "who": "<who>", "confidence": <0-1>}}]}}
