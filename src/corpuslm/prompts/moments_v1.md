---
id = "moments"
version = 1
stage = "moments"
output_schema = "moments_reply"
config_section = "moments"
---
You read the claims of {source_label} and pick out its moments: the few
stretches where something worth retelling happens, an insight that could
carry a short monologue on its own. A moment is a span of the source, usually
one to three minutes of talk or a few paragraphs, whose claims together make
one point. Most of a source is not a moment; pick at most {max_moments}, and
fewer when the source has fewer.

TITLE: {title}
DATE: {date}

CLAIMS (one per line, as `[index] (segments first-last) text`, in order):
{claims}

For each moment:
- `insight`: one plain sentence of at most 25 words saying what the moment
  shows, in the speaker's own terms, retold generically: a person is "my son"
  or "a friend", never a name.
- `claims`: the indexes of the claims the moment draws on, at least one; the
  moment's span runs from the first to the last segment those claims cite.

Reply with ONE JSON object and nothing else:

{{
  "moments": [
    {{"insight": "<sentence>", "claims": [<index>]}}
  ]
}}
