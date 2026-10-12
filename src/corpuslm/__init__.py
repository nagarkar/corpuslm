"""corpuslm: the source-agnostic transcript corpus core shared by ytlm and beelm.

It owns the schema of the shared transcript database (docs/design/storage-schema.md): the
tables, the migrations, `user_version` and the repository (`corpuslm.store.Store`). Tools never
write SQL against the file.

Terms:
- **owner**: the person who owns the corpora and gives review verdicts. (The `owner` column of
  `artifacts` and `locks` is a stored legacy name for something else: the source or corpus id the
  row belongs to; code calls it `source_or_corpus_id` or `locked_id`.)
- **source**: one transcribed item, a row of `sources` of a kind: `youtube` (a video, id the
  YouTube id) or `bee` (a conversation, id `bee:<conversation id>`). Its tags, topics and people
  are rows of `source_tags`, `source_topics` and `source_people`. In code `source_id` is the id
  and `source_row` the row. ytlm says "video" for a `youtube` source. The stored `video_id` keys
  in documents and some columns name a source id.
- **feed**: a YouTube playlist or channel the owner tracks as an inbox, a row of `feeds` of kind
  `playlist` or `channel`. A feed is never a corpus. The `source_feeds` rows of a source say which
  feeds it was found through.
- **feed skip**: a video the owner set aside from a feed's scan, a row of `source_skips` with a
  reason and a date; lifted by setting `lifted_at`, never deleted.
- **corpus**: a named set of sources, a row of `corpora` (with `corpus_topics` and
  `corpus_perspectives`). Plural `corpora`.
- **member**: a source in a corpus, a row of `corpus_members` with a reason (`author` (stored
  name for the owner), `discovered`, `consumer`); the member's status (`discovered`, `queued`,
  `ingested`, `failed`, `ignored`) is derived in the view `vw_corpus_members`.
- **ignored**: a source whose status is `ignored`: the owner set it aside. It stays in the catalog
  (a feed scan never offers it again) and keeps its stored files, segments and artifacts, but is
  excluded everywhere the owner works from: never built, never pending or billable, no review
  cards, no open items, no search hits, no member listing, no AI cleanup; corpus boards count it
  apart (`ignored`) and leave it out of the member total. Reversible: the tool returns it to
  `ingested` when it has a built transcript, else `added`.
- **segment**: one timed line of a transcript (`idx`, `t0`, `t1`), never re-cut: edits stay inside it.
  `segments` keeps the raw lines, never overwritten;
  `cleaned_segments` keeps the cleaned lines the search index reads (view `vw_search_segments`).
- **artifact**: a file one node of the pipeline built for a source or corpus, a row of
  `artifacts` with its path, sha256, state (`built`, `stale`, `failed`, `skipped`), the hashes of
  its inputs and its provenance.
- **answered through**: where a verdict was answered: a vault note (stored as `review file`) or
  the review page (stored as `phone page`), column `review_verdicts.source`.
- **review verdict**: the owner's answer to one review item, an append-only row of
  `review_verdicts`; the view `vw_current_review_verdicts` holds the newest per item.
- **speaker verdict**: the owner's name for one speaker label of a source, an append-only row of
  `speaker_verdicts` (current ones in `vw_current_speaker_verdicts`).
- **correction**: one correction memory rule, an append-only row of `corrections` (current ones
  in `vw_current_corrections`); kind `term` or `reading` (the stored name of the
  ordinary-correction kind).
- **review answer**: what the review page saved and `review apply` has not yet landed, a row of
  `review_answers`.
- **model suggestion**: the AI cleanup's pick for one ambiguity, recorded with its candidates in
  `model_suggestions` and `model_suggestion_candidates` so the review page can show it without
  the model (the artifact calls the same choice `pick`).
- **fetch attempt**: one try to get a video's captions, an append-only row of `fetch_attempts`.
- **run**: one execution of a tool's verb that builds things, a row of `runs`.
- **change log**: the append-only history of every master table's rows, `change_log`, written by
  triggers.
- **lock**: a row of `locks` saying which holder is working on a source or corpus until it
  expires.
- **dry run**: a verb called with `--dry-run`: it says what it would do and changes and spends
  nothing; its board status is `dry-run`.
- **build step**: the stage of the build order (1 to 4) in which a pipeline node is built;
  `Node.build_step`.
"""

SCHEMA_VERSION = "5"
