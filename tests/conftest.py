"""Tests never touch a real database (metalm coding.md, "Sharing one
database between repos": the scrub removes each repo's pointer) and never
inherit a git hook's environment."""

import os

for key in [
    variable_name
    for variable_name in os.environ
    if variable_name.startswith("GIT_") or variable_name.endswith("_DB")
]:
    os.environ.pop(key, None)
