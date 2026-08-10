import json
from pathlib import Path

from benchmarks import score
from benchmarks.report import _HEADLINE_TASKS
from benchmarks.validate import MEMORY_HEADLINE_TASKS

TASKS = json.loads(Path("benchmarks/memory_tasks.json").read_text())


def test_memory_tasks_schema_and_ids():
    ts = TASKS
    assert {t["id"] for t in ts} == {"rat-mem", "sup-current", "cont-log", "enf-commit"}
    for t in ts:
        assert t["seed_decision"] == ""   # organic seeding only
        assert t["scorer"] in {"rationale", "sup_current", "violations", "enforcement"}
    sup = next(t for t in ts if t["id"] == "sup-current")
    assert "first line" in sup["prompt"]
    assert sup["headline"] is True


def test_headline_flag_matches_both_consumers():
    """report.py and validate.py each re-spell the headline ids as a literal tuple;
    the JSON's "headline" flag is the source of truth for both."""
    flagged = tuple(t["id"] for t in TASKS if t["headline"])
    assert set(flagged) == set(_HEADLINE_TASKS) == set(MEMORY_HEADLINE_TASKS)


def test_rat_mem_gold_is_variant_tolerant():
    """The gold must not reward verbatim echoing of Contexer's stored sentence: a
    paraphrase carrying the same facts has to score 1.0."""
    gold = next(t for t in TASKS if t["id"] == "rat-mem")["gold"]
    assert len(gold) >= 2 and all(isinstance(g, list) for g in gold)
    paraphrase = ("Errors come back as a dictionary holding a code and a message, "
                  "which keeps failures machine readable for clients.")
    verbatim = ("API errors are returned as dicts with code and message keys so "
                "clients get machine-parseable failures.")
    assert score.rationale_score(paraphrase, gold) == 1.0
    assert score.rationale_score(verbatim, gold) == 1.0
    assert score.rationale_score("It uses Postgres.", gold) < 1.0
