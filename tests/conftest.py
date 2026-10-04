"""Tests never touch a real database (metalm coding.md, "Sharing one
database between repos": the scrub removes each repo's pointer) and never
inherit a git hook's environment."""

import os

for key in [k for k in os.environ if k.startswith("GIT_") or k.endswith("_DB")]:
    os.environ.pop(key, None)
