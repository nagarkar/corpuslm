"""The shared command-line helpers (the corpuslm.clikit rules): usage errors exit 1, help and
completion are generated from the parser, global options work anywhere on the line."""

import subprocess

import pytest

from corpuslm import clikit


def _parser():
    top = clikit.Parser(prog="demo")
    sub = top.add_subparsers(dest="verb", parser_class=clikit.Parser)
    one = sub.add_parser("status", help="show the counts")
    clikit.describe(one, "Show the counts.", "demo status")
    group = sub.add_parser("corpus", help="corpora: add")
    inner = group.add_subparsers(dest="corpus_verb", parser_class=clikit.Parser)
    add = inner.add_parser("add", help="add a corpus")
    add.add_argument("--person")
    clikit.describe(add, "Add one.", "demo corpus add --person X")
    return top


def test_a_usage_error_exits_one():
    with pytest.raises(SystemExit) as raised:
        _parser().parse_args(["bogus"])
    assert raised.value.code == 1


def test_help_lists_every_leaf_and_walks_down_to_a_subverb():
    text, code = clikit.help_text(_parser(), "demo")
    assert code == 0 and "status" in text and "corpus add" in text
    text, code = clikit.help_text(_parser(), "demo", ["corpus", "add"])
    assert code == 0 and "example:" in text and "--person" in text
    text, code = clikit.help_text(_parser(), "demo", ["corpus", "nope"])
    assert code == 1 and "unknown verb" in text


def test_global_options_are_pulled_out_wherever_they_stand():
    rest, flags, values = clikit.extract_options(
        ["status", "--json", "--set", "a.b=1", "--set=c.d=2", "--", "--json"], valued=("--set",)
    )
    assert rest == ["status", "--", "--json"]
    assert flags == {"--json"} and values["--set"] == ["a.b=1", "c.d=2"]


def test_completion_offers_verbs_subverbs_and_the_global_flags():
    script = clikit.bash_completion(_parser(), "demo", ["demo", "./demo.sh"], ("--json",))
    probe = f'{script}\nCOMP_WORDS=(demo corpus ""); COMP_CWORD=2; _demo; echo "${{COMPREPLY[@]}}"'
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert "add" in out and "--json" in out
    probe = f'{script}\nCOMP_WORDS=(demo ""); COMP_CWORD=1; _demo; echo "${{COMPREPLY[@]}}"'
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert "status" in out and "corpus" in out
