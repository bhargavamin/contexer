"""The agent-guidance files stay an index plus on-demand detail, and their links stay valid.

The root CLAUDE.md is injected into every coding-agent session in this repo. It grew to 73 KB
(~20k tokens), most of it per-module detail a given task never needs, so that detail moved to
docs/architecture.md and two folder guides that Claude Code loads only when files there are read.
These checks keep it from growing back and keep the moved text reachable.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "CLAUDE.md"
GUIDES = [ROOT, REPO / "docs/architecture.md", REPO / "contexer/adapters/CLAUDE.md",
          REPO / "benchmarks/CLAUDE.md", REPO / "AGENTS.md"]

# Measured 17.7 KB when the split landed; Claude Code's docs warn at ~40,000 characters. The cap
# leaves room for a few index lines, not for a subsystem's worth of prose.
ROOT_MAX_CHARS = 25_000

# Section names that code comments, tests or other docs cite as "CLAUDE.md, <name>".
CITED_IN_ROOT = [
    "## Working in this repo",
    "## Commands",
    "## Prompt-capture benchmark maintenance",
    "## Module boundaries",
    "## Design constraints",
    "**Secrets never egress; capture stays faithful.**",
    "**Silent operation is essential.**",
    "no `Stop` hook",
    "bookkeeping writes are best-effort",
    "never blocks a commit on its own failure",
]

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(line.lstrip("#")) for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")}


def test_root_guide_stays_an_index():
    size = len(ROOT.read_text(encoding="utf-8"))
    assert size <= ROOT_MAX_CHARS, (
        f"CLAUDE.md is {size} chars (cap {ROOT_MAX_CHARS}). It loads into every agent session: "
        "move module detail to docs/architecture.md or the module docstring and leave one index "
        "line here.")


def test_cited_sections_stay_in_the_root_guide():
    text = ROOT.read_text(encoding="utf-8")
    missing = [name for name in CITED_IN_ROOT if name not in text]
    assert missing == [], missing


def test_every_package_module_has_an_index_line():
    """A new module must be findable from the always-loaded index, not only from the reference."""
    text = ROOT.read_text(encoding="utf-8")
    modules = {p.stem for p in (REPO / "contexer").glob("*.py")
               if not p.stem.startswith("_")}
    # Supporting modules the index intentionally leaves to CONTRIBUTING.md's structure table.
    unindexed_by_design = {"auth", "config", "decision_observability", "hook_host", "share",
                           "share_policy", "sidecars"}
    missing = sorted(m for m in modules - unindexed_by_design if f"`{m}.py`" not in text)
    assert missing == [], f"add a one-line index entry to CLAUDE.md for: {missing}"


def test_relative_links_and_anchors_resolve():
    broken = []
    for guide in GUIDES:
        for target in _LINK.findall(guide.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            dest = (guide.parent / path_part).resolve() if path_part else guide
            if not dest.exists():
                broken.append(f"{guide.relative_to(REPO)} -> {target}")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                broken.append(f"{guide.relative_to(REPO)} -> {target} (no such heading)")
    assert broken == [], broken


def test_agents_md_names_the_folder_guides():
    """Codex and other agents do not auto-load nested CLAUDE.md files, so AGENTS.md must name them."""
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    for guide in ("docs/architecture.md", "contexer/adapters/CLAUDE.md", "benchmarks/CLAUDE.md"):
        assert guide in text, guide
