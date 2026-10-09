"""Speaker schemes for tests: a scheme shaped like ytlm's YouTube one (HOST and GUEST_n,
assigned by the model) and Bee's (fixed labels from the recorder). ytlm's own scheme lives in
ytlm; this copy is what the parametrized tests run beside `bee_scheme`."""

from corpuslm.speaker_map import SpeakerScheme, bee_scheme

YOUTUBE_LIKE = SpeakerScheme(
    labels=("HOST", "GUEST_1", "GUEST_2", "GUEST_3", "GUEST_4", "GUEST_5", "GUEST_6", "UNKNOWN"),
    unresolved_label="UNKNOWN",
    resolve_default=lambda label, source: (
        (source.get("channel_title") or None) if label == "HOST" else None
    ),
    prompt_id="cleanup",
    fixed_labels=False,
)
OWNER = "Quill"
BEE = bee_scheme(OWNER)

# Two labels each: the first resolves by default, the second never does.
LABELS = {"youtube": ("HOST", "GUEST_1"), "bee": (OWNER, "Speaker 2")}
SCHEMES = {"youtube": YOUTUBE_LIKE, "bee": BEE}
SOURCE = {"id": "src-1", "channel_title": "Some Channel"}
