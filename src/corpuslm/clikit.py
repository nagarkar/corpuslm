"""Clikit: the generic parts of a command line that ytlm and beelm share.

argparse helpers with no tool in them: a parser whose usage error exits 1, a description and
example on every verb, the flattened verb list, `help [verb [subverb]]` text, global options
accepted anywhere on the line (`--json`, `--set X`), and a bash completion script generated
from the parser each time it is printed. A tool passes its own name and parser.

Never imports: db, store (it knows nothing of the database).

Decisions:
- A usage error exits 1, not argparse's 2: the house exit codes are 0 ok and 1 error
  (metalm cli.md). Enforced by: tests/test_clikit.py::test_a_usage_error_exits_one.
- Completion is generated from the parser, never kept by hand. Enforced by:
  tests/test_clikit.py::test_completion_offers_verbs_subverbs_and_the_global_flags.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


class Parser(argparse.ArgumentParser):
    """argparse whose usage error exits 1."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def describe(parser: argparse.ArgumentParser, description: str, example: str) -> None:
    """Give a verb its description and its example (metalm cli.md: every verb has both)."""
    parser.description = description
    parser.epilog = f"example:\n  {example}"
    parser.formatter_class = argparse.RawDescriptionHelpFormatter


def subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def verb_summaries(parser: argparse.ArgumentParser, prefix: str = "") -> list[tuple[str, str]]:
    """(verb, one-line help) for every leaf, grouped verbs flattened as 'corpus add'."""
    out: list[tuple[str, str]] = []
    action = subparsers(parser)
    if action is None:
        return out
    helps = {choice_action.dest: choice_action.help for choice_action in action._choices_actions}
    for name, sub in action.choices.items():
        full = f"{prefix}{name}"
        if subparsers(sub):
            out += verb_summaries(sub, full + " ")
        else:
            out.append((full, helps.get(name, "") or ""))
    return out


def help_text(
    parser: argparse.ArgumentParser,
    prog: str,
    topic: Sequence[str] | None = None,
    footer: Sequence[str] = (),
) -> tuple[str, int]:
    """The help text and an exit code (1 for an unknown verb)."""
    if not topic:
        lines = [f"Verbs (type '{prog} help <verb>' for options):"]
        for verb, summary in verb_summaries(parser):
            lines.append(f"  {verb:<16} {summary}")
        if footer:
            lines += ["", *footer]
        return "\n".join(lines) + "\n", 0
    node = parser
    walked: list[str] = []
    for part in topic:
        action = subparsers(node)
        if action is None or part not in action.choices:
            known = " ".join(walked) if walked else None
            hint = (
                f"Type '{prog} help {known}' for its subverbs."
                if known
                else f"Type '{prog} help' for the list."
            )
            return f"error: unknown verb '{' '.join(topic)}'. {hint}\n", 1
        node = action.choices[part]
        walked.append(part)
    text = node.format_help()
    action = subparsers(node)
    if action is not None:
        text += f"\nsubverbs (type '{prog} help " + " ".join(walked) + " <subverb>'):\n"
        for verb, summary in verb_summaries(node, " ".join(walked) + " "):
            text += f"  {verb:<16} {summary}\n"
    return text, 0


def extract_options(
    argv: Sequence[str], flags: Sequence[str] = ("--json",), valued: Sequence[str] = ()
) -> tuple[list[str], set[str], dict[str, list[str]]]:
    """Pull global options out of the line wherever they stand.

    Returns the rest, the flags seen, and the values of each valued option (`--set X` or
    `--set=X`). A literal argument after `--` is left alone."""
    rest: list[str] = []
    seen: set[str] = set()
    values: dict[str, list[str]] = {name: [] for name in valued}
    argv = list(argv)
    argument_index = 0
    while argument_index < len(argv):
        argument = argv[argument_index]
        if argument == "--":
            rest += argv[argument_index:]
            break
        if argument in flags:
            seen.add(argument)
        elif argument in valued and argument_index + 1 < len(argv):
            values[argument].append(argv[argument_index + 1])
            argument_index += 1
        elif any(argument.startswith(option_name + "=") for option_name in valued):
            name = next(
                option_name for option_name in valued if argument.startswith(option_name + "=")
            )
            values[name].append(argument[len(name) + 1 :])
        else:
            rest.append(argument)
        argument_index += 1
    return rest, seen, values


def completion_tree(parser: argparse.ArgumentParser, path: str = "") -> dict[str, list[str]]:
    """path ('' for the top, 'corpus', 'corpus add') -> the words that may follow it there."""
    words: list[str] = []
    out: dict[str, list[str]] = {}
    action = subparsers(parser)
    if action is not None:
        for name, sub in action.choices.items():
            words.append(name)
            out |= completion_tree(sub, f"{path} {name}".strip())
    for parser_action in parser._actions:
        words += [option for option in parser_action.option_strings if option.startswith("--")]
    out[path] = words
    return out


def bash_completion(
    parser: argparse.ArgumentParser,
    prog: str,
    launchers: Sequence[str],
    global_flags: Sequence[str] = ("--json",),
) -> str:
    """A bash completion script generated from the parser each time it is printed (bash 3.2
    compatible: no associative arrays). The global flags are offered everywhere."""
    tree = completion_tree(parser)
    cases = "\n".join(
        f'    "{command}") echo "{" ".join(words)}" ;;' for command, words in sorted(tree.items())
    )
    known = "|".join(f'"{command}"' for command in sorted(tree) if command)
    shell_id = prog.replace("-", "_")
    flags = " ".join(global_flags)
    names = " ".join(launchers)
    return f"""# {prog} bash completion, generated from the argument parser.
# Load it with: eval "$({launchers[-1]} completion bash)"
_{shell_id}_words() {{
  case "$1" in
{cases}
  esac
}}
_{shell_id}_known() {{
  case "$1" in
    {known}) return 0 ;;
    *) return 1 ;;
  esac
}}
_{shell_id}() {{
  local path="" next w i
  for ((i = 1; i < COMP_CWORD; i++)); do
    w="${{COMP_WORDS[i]}}"
    case "$w" in -*) continue ;; esac
    if [ -z "$path" ]; then next="$w"; else next="$path $w"; fi
    if _{shell_id}_known "$next"; then path="$next"; fi
  done
  COMPREPLY=($(compgen -W "$(_{shell_id}_words "$path") {flags}" -- "${{COMP_WORDS[COMP_CWORD]}}"))
}}
complete -F _{shell_id} {names}
"""
