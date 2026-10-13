"""Monologues: the first artifact, a script made from a set of moments by a recipe, inside the
perspective of the bodies of work it draws on.

A monologue is a named set of moments (by source and span) and bodies of work, with a brief. A
RECIPE is a versioned prompt (`monologue`, corpuslm.prompts) that turns the moments, the
standing bodies' perspective sheets and the occasional bodies' claims into a script. A run's
INGREDIENTS are everything the output should reflect: the recipe prompt's hash, the model, the
moment set with each moment's insight and claims, and each body with its sheet version (standing)
or its claims build key (occasional). The ingredients hash keys `monologue_runs`: the same
ingredients return the stored run and spend nothing (nagarkar/corpuslm#50, milestone "Artifacts:
monologues").

The PRIOR is passed unhashed: the settled text of the latest script tab in the monologue's Doc,
with the owner's edits, as a style and shape guide, with a note of what changed in the
ingredients since that run; the recipe keeps the prior's voice, structure and phrasing where the
new ingredients do not require otherwise and makes no gratuitous change. `--fresh` drops the
prior for one run. Each run becomes a new tab of the monologue's own Doc (`Script v<n>`) through
the document store; comments there go through authorlm's comment workflow and never trigger a
run. `status` names what changed since the latest run.

Never imports: cli, clikit. Reads the database only through `Store`; reaches the Doc only
through `corpuslm.document`.

Decisions:
- Nothing regenerates when a run for the same ingredients exists. Enforced by:
  tests/test_monologues.py::test_the_same_ingredients_return_the_stored_run_and_spend_nothing.
- The prior output, with the owner's edits, is a style and shape guide passed unhashed: new
  sections are allowed when the sheet or prompt calls for them, gratuitous changes to look and
  feel are not. (owner)
- Comments on a monologue Doc go through authorlm's comment workflow and never trigger a run.
  (owner)
- A monologue retells generically: no family names. (owner)

Terms:
- **monologue**: a named set of moments and bodies of work with a brief, a row of `monologues`,
  whose runs are scripts.
- **recipe**: the versioned prompt that turns ingredients into a script (here `monologue`).
- **ingredients**: what a run's output reflects, hashed: recipe, model, moments with their
  claims, bodies with their sheet version or claims key.
- **prior**: the latest script tab's settled text, the style guide for the next run; not
  hashed.
- **run**: one execution of the recipe, a row of `monologue_runs` and a script tab.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import bodies as bodies_module
from . import document, llm, prompts
from . import moments as moments_module
from .identity import canonical_sha256
from .store import Store

STAGE = "monologue"
ABOUT = "about.md"
DEFAULT_MINUTES = 4
WORDS_PER_MINUTE = 150
EXCERPT_CHARS = 2500  # of a moment's span shown to the model
REFERENCE_CLAIMS = 40  # per occasional body
OMIT_OPEN, OMIT_CLOSE = bodies_module.OMIT_OPEN, bodies_module.OMIT_CLOSE


def prompt() -> prompts.Prompt:
    return prompts.load("monologue")


def settings(config: dict) -> dict:
    section = config.get("monologue") or {}
    if not section.get("model"):
        raise llm.NotConfigured("[monologue].model is missing from the settings file")
    return {"model": str(section["model"]), "minutes": int(section.get("minutes", DEFAULT_MINUTES))}


def slug(name: str) -> str:
    made = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not made:
        raise ValueError(f"the name {name!r} leaves no letters or digits")
    return made


# ---- the monologue -------------------------------------------------------------------------


def create(store: Store, name: str, *, title: str = "", brief: str = "", created_by: str) -> dict:
    name = slug(name)
    if store.monologue(name):
        raise ValueError(f"a monologue named {name!r} exists")
    made = store.add_monologue(name, title=title, brief=brief, created_by=created_by)
    return {"id": made, "name": name}


def _monologue(store: Store, name: str) -> dict:
    row = store.monologue(slug(name))
    if row is None:
        raise KeyError(f"no monologue named {name!r}")
    return row


def attach(
    store: Store, name: str, *, moment_id: str | None = None, body: str | None = None
) -> dict:
    row = _monologue(store, name)
    if (moment_id is None) == (body is None):
        raise ValueError("attach one moment (--moment) or one body of work (--body)")
    if moment_id is not None:
        moment = store.moment(moment_id)
        if moment is None:
            raise KeyError(f"no moment {moment_id!r}")
        verdict = store.current_moment_verdicts(moment["source_id"]).get(moment["subject_sha256"])
        if moments_module.state_of(verdict) != "approved":
            raise ValueError(f"moment {moment_id!r} is not approved; approve it first")
        added = store.attach_moment(row["id"], moment["source_id"], moment["subject_sha256"])
        return {"monologue": row["name"], "moment": moment_id, "attached": added}
    found = store.body(body or "")
    if found is None:
        raise KeyError(f"no body of work named {body!r}")
    added = store.attach_body(row["id"], found["id"])
    return {"monologue": row["name"], "body": found["name"], "attached": added}


def detach(
    store: Store, name: str, *, moment_id: str | None = None, body: str | None = None
) -> dict:
    row = _monologue(store, name)
    if moment_id is not None:
        moment = store.moment(moment_id)
        if moment is None:
            raise KeyError(f"no moment {moment_id!r}")
        gone = store.detach_moment(row["id"], moment["source_id"], moment["subject_sha256"])
        return {"monologue": row["name"], "moment": moment_id, "detached": gone}
    found = store.body(body or "")
    if found is None:
        raise KeyError(f"no body of work named {body!r}")
    return {
        "monologue": row["name"],
        "body": found["name"],
        "detached": store.detach_body(row["id"], found["id"]),
    }


# ---- the ingredients ------------------------------------------------------------------------


def _moment_claims(store: Store, moment: dict) -> list[str]:
    return [claim["text"] for claim in store.moment_claims(moment)]


def _sheet_text(doc: document.DocumentStore, body: dict) -> str:
    """The sheet tab's settled text without its wrapper lines; '' when there is none."""
    if not body["manuscript"]:
        return ""
    try:
        tabs = {tab["file"]: tab for tab in doc.read(body["manuscript"])["tabs"]}
    except document.DocumentUnavailable:
        return ""
    tab = tabs.get(bodies_module.SHEET_FILE)
    if tab is None:
        return ""
    blocks = [
        block
        for block in document.paragraphs(tab["settled"])
        if block not in (OMIT_OPEN, OMIT_CLOSE) and not block.startswith("# ")
    ]
    return "\n\n".join(blocks)


def ingredients(store: Store, doc: document.DocumentStore, name: str, config: dict) -> dict:
    """Everything the output should reflect, as hashed. Reads each standing body's sheet so its
    version is current."""
    row = _monologue(store, name)
    found = settings(config)
    moments = [
        {
            "source": moment["source_id"],
            "subject": moment["subject_sha256"],
            "insight": moment["insight"],
            "claims": _moment_claims(store, moment),
        }
        for moment in store.monologue_moments(row["id"])
    ]
    bodies = []
    for body in store.monologue_bodies(row["id"]):
        if body["kind"] == "standing":
            version = (
                bodies_module.status(store, doc, body["name"])["version"]
                if body["manuscript"]
                else ""
            )
            bodies.append({"name": body["name"], "kind": "standing", "sheet": version})
        else:
            source_ids = bodies_module.source_ids_of(store, body)
            keys = [
                (store.artifact(source_id, "claims") or {}).get("sha256", "")
                for source_id in source_ids
            ]
            bodies.append({"name": body["name"], "kind": "occasional", "claims": keys})
    return {
        "recipe": {"id": prompt().id, "version": prompt().version, "sha256": prompt().sha256},
        "model": found["model"],
        "minutes": found["minutes"],
        "brief": row["brief"],
        "moments": moments,
        "bodies": bodies,
    }


def ingredients_sha256(record: dict) -> str:
    return canonical_sha256(record)


def changes_between(old: dict, new: dict) -> list[str]:
    """What differs, named for the owner and the recipe."""
    out = []
    if old.get("recipe", {}).get("sha256") != new["recipe"]["sha256"]:
        out.append(
            f"recipe {old.get('recipe', {}).get('version', '?')} to {new['recipe']['version']}"
        )
    for key in ("model", "minutes", "brief"):
        if old.get(key) != new[key]:
            out.append(f"{key} changed")
    old_moments = {(m["source"], m["subject"]): m for m in old.get("moments", [])}
    new_moments = {(m["source"], m["subject"]): m for m in new["moments"]}
    added = len(set(new_moments) - set(old_moments))
    removed = len(set(old_moments) - set(new_moments))
    reclaimed = sum(
        1 for key in set(new_moments) & set(old_moments) if new_moments[key] != old_moments[key]
    )
    if added:
        out.append(f"{added} moment(s) added")
    if removed:
        out.append(f"{removed} moment(s) removed")
    if reclaimed:
        out.append(f"{reclaimed} moment(s) changed")
    old_bodies = {b["name"]: b for b in old.get("bodies", [])}
    new_bodies = {b["name"]: b for b in new["bodies"]}
    for body_name in sorted(set(new_bodies) - set(old_bodies)):
        out.append(f"body {body_name} attached")
    for body_name in sorted(set(old_bodies) - set(new_bodies)):
        out.append(f"body {body_name} detached")
    for body_name in sorted(set(old_bodies) & set(new_bodies)):
        if old_bodies[body_name] != new_bodies[body_name]:
            out.append(f"body {body_name} changed (its sheet or claims)")
    return out


@dataclass(frozen=True, slots=True)
class Status:
    name: str
    runs: int
    state: str  # current | stale | none
    changes: list[str]


def status(store: Store, doc: document.DocumentStore, name: str, config: dict) -> Status:
    row = _monologue(store, name)
    runs = store.monologue_runs(row["id"])
    current = ingredients(store, doc, name, config)
    if not runs:
        return Status(row["name"], 0, "none", [])
    latest = runs[-1]
    if latest["ingredients_sha256"] == ingredients_sha256(current):
        return Status(row["name"], len(runs), "current", [])
    return Status(
        row["name"], len(runs), "stale", changes_between(json.loads(latest["ingredients"]), current)
    )


# ---- the materials and the run -------------------------------------------------------------


def _excerpt(store: Store, moment: dict) -> str:
    segments = store.best_segments(moment["source_id"])
    lines = [
        (f"{s['speaker']}: " if s.get("speaker") else "") + s["text"]
        for s in segments
        if moment["first_position"] <= s["idx"] <= moment["last_position"]
    ]
    text = "\n".join(lines)
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + " ..."


def _variables(
    store: Store,
    doc: document.DocumentStore,
    row: dict,
    record: dict,
    prior: str,
    changes: list[str],
) -> dict:
    sheets = []
    for body in store.monologue_bodies(row["id"]):
        if body["kind"] != "standing":
            continue
        text = _sheet_text(doc, body)
        sheets.append(
            f"PERSPECTIVE SHEET OF {body['name'].upper()}"
            + (f" (the owner's note: {body['note']})" if body["note"] else "")
            + ":\n"
            + (text or "(no sheet yet)")
        )
    moments = []
    for number, moment in enumerate(store.monologue_moments(row["id"]), 1):
        concepts = ", ".join(store.moment_concept_keys(moment)) or "none tagged"
        claims = "\n".join(f"  - {text}" for text in _moment_claims(store, moment)) or "  (none)"
        moments.append(
            f"MOMENT {number}: {moment['insight']}\n  concepts: {concepts}\n  claims:\n{claims}\n"
            f"  what was said:\n{_excerpt(store, moment)}"
        )
    references = []
    for body in store.monologue_bodies(row["id"]):
        if body["kind"] != "occasional":
            continue
        texts = [
            claim["text"]
            for source_id in bodies_module.source_ids_of(store, body)
            for claim in store.claims(source_id)
        ][:REFERENCE_CLAIMS]
        references.append(f"{body['name']}:\n" + "\n".join(f"- {t}" for t in texts))
    if prior:
        prior_block = (
            "THE PRIOR SCRIPT (the owner's settled text; keep its voice, structure, length and "
            "phrasing wherever the ingredients do not require otherwise; add, remove or reshape "
            "sections only where they do; make no gratuitous change to its look and feel). What "
            "changed since it was written: "
            + ("; ".join(changes) or "nothing named")
            + "\n"
            + prior
        )
    else:
        prior_block = "THE PRIOR SCRIPT: no prior output; write from the ingredients."
    return {
        "minutes": str(record["minutes"]),
        "words": str(record["minutes"] * WORDS_PER_MINUTE),
        "sheets": "\n\n".join(sheets) or "PERSPECTIVE SHEET: none attached.",
        "brief": row["brief"] or "(none)",
        "moments": "\n\n".join(moments) or "(none attached)",
        "references": "\n\n".join(references) or "(none)",
        "prior": prior_block,
    }


def manuscript_name(row: dict) -> str:
    return f"monologue-{row['name']}"


def _latest_script(doc: document.DocumentStore, row: dict) -> tuple[str, str]:
    """(file, settled text) of the latest script tab; ('', '') when there is none."""
    if not row["manuscript"]:
        return "", ""
    try:
        tabs = [
            tab for tab in doc.read(row["manuscript"])["tabs"] if tab["file"].startswith("script-")
        ]
    except document.DocumentUnavailable:
        return "", ""
    if not tabs:
        return "", ""
    tabs.sort(key=lambda tab: int(re.sub(r"\D", "", tab["file"]) or 0))
    return tabs[-1]["file"], tabs[-1]["settled"]


def _prior_text(settled: str) -> str:
    return "\n\n".join(
        block for block in document.paragraphs(settled) if not block.startswith("# ")
    )


@dataclass(frozen=True, slots=True)
class Estimate:
    moments: int
    bodies: int
    tokens_in: int
    tokens_out: int
    usd: float | None
    model: str
    stored: bool  # a run for these ingredients exists: nothing would be sent


def estimate(
    store: Store, client: llm.Client, doc: document.DocumentStore, name: str, *, fresh: bool = False
) -> Estimate:
    row = _monologue(store, name)
    record = ingredients(store, doc, name, client.config)
    found = settings(client.config)
    if store.monologue_run_by_ingredients(row["id"], ingredients_sha256(record)):
        return Estimate(
            len(record["moments"]), len(record["bodies"]), 0, 0, 0.0, found["model"], True
        )
    runs = store.monologue_runs(row["id"])
    _, settled = _latest_script(doc, row)
    prior = "" if fresh else _prior_text(settled)
    changes = changes_between(json.loads(runs[-1]["ingredients"]), record) if runs else []
    variables = _variables(store, doc, row, record, prior, changes)
    tokens_in = max(1, len(prompt().render(variables)) // 4 + 1)
    tokens_out = record["minutes"] * WORDS_PER_MINUTE * 2
    return Estimate(
        len(record["moments"]),
        len(record["bodies"]),
        tokens_in,
        tokens_out,
        llm.price_usd(found["model"], tokens_in, tokens_out),
        found["model"],
        False,
    )


def run(
    store: Store,
    client: llm.Client,
    doc: document.DocumentStore,
    name: str,
    *,
    directory: Path,
    fresh: bool = False,
    created_by: str,
) -> dict:
    """Run the recipe unless a run for these ingredients exists; write the script as a new tab
    of the monologue's Doc and the about tab. Returns counts and the Doc's URL."""
    row = _monologue(store, name)
    record = ingredients(store, doc, name, client.config)
    key = ingredients_sha256(record)
    stored = store.monologue_run_by_ingredients(row["id"], key)
    if stored is not None:
        return {
            "monologue": row["name"],
            "run": stored["number"],
            "unchanged": True,
            "tab": stored["tab"],
            "cost_usd": 0.0,
            "note": "unchanged ingredients: the stored run stands; nothing was sent",
        }
    if not record["moments"]:
        raise ValueError(f"{row['name']} has no approved moments attached")
    runs = store.monologue_runs(row["id"])
    prior_file, settled = _latest_script(doc, row)
    prior = "" if fresh else _prior_text(settled)
    changes = changes_between(json.loads(runs[-1]["ingredients"]), record) if runs else []
    reply = client.complete(
        STAGE,
        prompt(),
        _variables(store, doc, row, record, prior, changes),
        tag=f"{row['name']}-{key[:12]}",
    )
    paragraphs = [" ".join(text.split()) for text in reply.parsed["paragraphs"] if text.strip()]
    output = "\n\n".join(paragraphs)
    title = " ".join(reply.parsed["title"].split())
    number = len(runs) + 1
    tab = f"script-{number}.md"
    manuscript = row["manuscript"] or manuscript_name(row)
    made = doc.ensure(manuscript, Path(directory), row["title"] or title)
    if not row["manuscript"]:
        store.update_monologue(row["name"], {"manuscript": manuscript, "directory": str(directory)})
    if not row["title"]:
        store.update_monologue(row["name"], {"title": title})
    doc.add_tab(manuscript, tab, f"Script v{number}")
    doc.write_tab(manuscript, tab, f"# Script v{number}: {title}\n\n{output}\n")
    run_id = store.add_monologue_run(
        row["id"],
        {
            "ingredients_sha256": key,
            "ingredients": json.dumps(record, sort_keys=True),
            "recipe": prompt().id,
            "recipe_sha256": prompt().sha256,
            "model": record["model"],
            "prior_run_id": runs[-1]["id"] if (runs and not fresh) else "",
            "fresh": int(fresh),
            "title": title,
            "output": output,
            "tab": tab,
            "tokens_in": reply.tokens_in,
            "tokens_out": reply.tokens_out,
            "cost_usd": round(reply.cost_usd or 0.0, 6),
        },
        created_by=created_by,
    )
    tabs = {tab["file"] for tab in doc.read(manuscript)["tabs"]}
    if ABOUT not in tabs:
        doc.add_tab(manuscript, ABOUT, "About this monologue")
    doc.write_tab(manuscript, ABOUT, about_text(store, store.monologue(row["name"]) or row))
    for body in store.monologue_bodies(row["id"]):
        store.update_body(
            body["name"], {"last_used_at": store.monologue_runs(row["id"])[-1]["created_at"]}
        )
    return {
        "monologue": row["name"],
        "run": number,
        "run_id": run_id,
        "unchanged": False,
        "fresh": fresh,
        "prior": prior_file,
        "changes": changes,
        "tab": tab,
        "paragraphs": len(paragraphs),
        "words": len(output.split()),
        "url": made["url"],
        "cached": reply.cached,
        "cost_usd": round(reply.cost_usd or 0.0, 6),
    }


def script_text(doc: document.DocumentStore, row: dict) -> tuple[str, str, int]:
    """(tab file, settled script text without its heading, open revisions) of the latest script
    tab; ('', '', 0) when there is none."""
    file, settled = _latest_script(doc, row)
    if not file:
        return "", "", 0
    tabs = {tab["file"]: tab for tab in doc.read(row["manuscript"])["tabs"]}
    return file, _prior_text(settled), len(tabs[file]["open"])


def export(store: Store, doc: document.DocumentStore, name: str, to: Path | None = None) -> dict:
    """Write the latest script tab's settled text as plain text, headed by the monologue's id and
    title so podlm can record where an episode came from (nagarkar/podlm#33). Returns the path
    and counts; open revisions are reported, and the settled text is what is exported."""
    row = _monologue(store, name)
    file, text, open_revisions = script_text(doc, row)
    if not file:
        raise ValueError(f"{row['name']} has no script yet; run it first")
    path = Path(to) if to else Path(row["directory"]) / f"{row['name']}.teleprompter.txt"
    header = f"monologue: {row['id']}\nname: {row['name']}\ntitle: {row['title']}\ntab: {file}\n\n"
    from .files import write_atomic

    write_atomic(path, header + text + "\n")
    return {
        "monologue": row["name"],
        "path": str(path),
        "tab": file,
        "words": len(text.split()),
        "open_revisions": open_revisions,
        "note": "the Doc has open revisions; the settled text was exported"
        if open_revisions
        else "",
    }


def about_text(store: Store, row: dict) -> str:
    """The code-owned about tab: what the monologue is made of and its runs."""
    lines = ["# About this monologue", "", OMIT_OPEN, "", f"Name: {row['name']}"]
    if row["title"]:
        lines.append(f"Working title: {row['title']}")
    if row["brief"]:
        lines.append(f"Brief: {row['brief']}")
    lines += ["", "## Moments"]
    for moment in store.monologue_moments(row["id"]):
        lines.append(f"- {moment['source_id']} segments {moment['first_position']} to "
                     f"{moment['last_position']}: {moment['insight']}")  # fmt: skip
    lines += ["", "## Bodies of work"]
    for body in store.monologue_bodies(row["id"]):
        lines.append(f"- {body['name']} ({body['kind']})")
    lines += ["", "## Runs"]
    for run_row in store.monologue_runs(row["id"]):
        lines.append(
            f"- v{run_row['number']} on {run_row['created_at'][:10]}: {run_row['title']} "
            f"({run_row['model']}, ingredients {run_row['ingredients_sha256'][:12]})"
        )
    lines += ["", OMIT_CLOSE, ""]
    return "\n".join(lines)


def listing(store: Store, doc: document.DocumentStore, config: dict | None) -> list[dict]:
    out = []
    for row in store.monologues():
        runs = store.monologue_runs(row["id"])
        entry = {
            "name": row["name"],
            "title": row["title"],
            "state": row["state"],
            "moments": len(store.monologue_moments(row["id"])),
            "bodies": [body["name"] for body in store.monologue_bodies(row["id"])],
            "runs": len(runs),
        }
        if config is not None:
            try:
                standing = status(store, doc, row["name"], config)
                entry["ingredients"] = standing.state
                entry["changes"] = standing.changes
            except llm.NotConfigured:
                pass
        out.append(entry)
    return out


__all__ = [
    "Estimate",
    "Status",
    "about_text",
    "attach",
    "changes_between",
    "create",
    "detach",
    "estimate",
    "export",
    "ingredients",
    "ingredients_sha256",
    "listing",
    "manuscript_name",
    "run",
    "script_text",
    "settings",
    "slug",
    "status",
]
