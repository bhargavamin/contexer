"""Served-tool regressions for Claude Code 2.1.296's measured 4,096-char boundary.

The projection below is a regression model of that boundary, not host validation.
Real-host request captures and the separate --bare limitation are recorded in
issue #404's validation report. Normalize whitespace only for clause matching
and baseline description compatibility; length checks use the actual served text.
"""
import asyncio
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from contexer import server

HOST_LIMIT = 4096
UPDATE_BUDGET = 3800

# Complete clauses keep the action, condition, and timing together. Review this
# coverage map when rewording; a keyword alone cannot establish contract visibility.
CONTRACT = {
    "capture": "Called when Claude Code makes a significant decision mid-task. The server filters before storing.",
    "comprehension": "A synthesized understanding of how a subsystem works - produced by exploring or reading the codebase to answer a question - is also capture-worthy (subtype='architecture' for subsystem behaviour/structure, 'pattern' for recurring code organization); store it in the SAME turn as the exploration, since sessions often end right after the answer and there may be no next prompt to catch it.",
    "subtypes": "subtype: optional classification - architecture | constraint | pattern | convention.",
    "provenance": "'plan' (approved but unimplemented plan: PROVISIONAL/suggested until implementation validates it, then reconciled)",
    "bootstrap": "New bootstrap findings use bootstrap_context's evidence/report workflow.",
    "revision_target": "replace_id: existing decision ID (full UUID or 8-char prefix); bypasses similarity filtering.",
    "history": "Decisions are versioned, never overwritten.",
    "trivial_revision": "A trivial change (typo/formatting, pattern/convention) becomes a new revision in place, keeping history.",
    "significant_revision": "A significant change (architecture/constraint) becomes a Suggested Update attached to the live decision; the current revision stays trusted until the developer approves.",
    "anchor_scope": "source_files: repo-relative files or directory prefixes (max 10) described by this decision.",
    "directory_prefix": "Use trailing slashes for prefixes, e.g. `contexer/`; existing directories are normalized automatically.",
    "comprehension_anchors": "Include described files for comprehension summaries so changed code can flag them as possibly stale.",
    "anchor_revision": "Anchors use fresh files + current HEAD on new decisions and replace_id corrections once corrected text is live:",
    "anchor_immediate": "immediately for trivial corrections or re-capture of still-accurate content;",
    "anchor_approval": "only on developer approval for significant corrections, while OLD content stays live.",
    "anchor_recurrence": "Never re-anchor on recurrence.",
    "staleness": 'For "[may be stale: ...]", re-read the named files and re-capture via replace_id with source_files again to refresh immediately or on approval.',
    "anchor_omission": "Omitting source_files keeps the old anchor and stale warning.",
    "title": "title: concise, one-line, imperative summary (<=100 chars)",
    "title_omission": "Omit only if you cannot summarize better than the store's derivation from content.",
    "applicability": 'applies_when: up to eight specific task phrases (2+ words, <=100 characters each) describing situations that need this decision, each with a situation-specific word (not only generic words like "fix test" or "new feature"). Use task vocabulary, for example ["slow upstream reads", "making fetches faster"]. They aid deterministic retrieval; they grant no approval or file authority. Omit when unknown.',
    "pending_status": "If this returns a 'pending review' notice, the decision is recorded but NOT yet trusted and does not block your work - keep going.",
    "pending_action": "Surface it to the developer for approval at a natural point (call approve_decision when they respond, or they can run `contexer review`); never discard it silently.",
    "review_listing": "Use review_pending to list everything awaiting review with its content.",
    "correction_failure": "If instead this returns a 'Correction NOT stored' notice, a higher-trust update already holds the decision's one proposal slot - do not retry the call; relay both versions to the developer that turn so they can review with full context.",
}


def _served_tools():
    # Same MCPTool serialization as tools/list, including metadata and JSON schemas.
    return [t.model_dump(mode="json", by_alias=True)
            for t in asyncio.run(server.mcp.list_tools())]


def _assert_lengths(tools):
    assert tools, "MCP tools/list must be nonempty"
    for tool in tools:
        size = len(tool.get("description") or "")
        assert size < HOST_LIMIT, f"{tool['name']}: {size} chars >= {HOST_LIMIT}"
        if tool["name"] == "update_context":
            assert size <= UPDATE_BUDGET, f"update_context: {size} chars > {UPDATE_BUDGET}"


def _assert_visible_contract(description):
    visible = " ".join(description[:HOST_LIMIT].split())
    for concept, clause in CONTRACT.items():
        assert clause in visible, f"update_context: missing visible {concept} clause"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _compatibility(tools):
    profiles = {}
    for tool in tools:
        metadata = {k: v for k, v in tool.items() if k != "description"}
        profile = {"metadata_sha256": _hash(metadata), "defaults_sha256": _hash({
            k: repr(v.default) for k, v in inspect.signature(
                getattr(server, tool["name"])).parameters.items()})}
        if tool["name"] != "update_context":
            profile["description_sha256"] = _hash(inspect.cleandoc(tool["description"] or ""))
        profiles[tool["name"]] = profile
    return {"tools": profiles, "instructions_sha256": _hash(server._INSTRUCTIONS)}


def test_all_served_tool_descriptions_fit_host_limit():
    _assert_lengths(_served_tools())


def test_update_context_contract_survives_default_host_boundary():
    tools = _served_tools()
    description = next(t["description"] for t in tools if t["name"] == "update_context")
    _assert_visible_contract(description)


def test_tool_schemas_defaults_metadata_and_other_guidance_match_baseline():
    # MCP 1.9.4 and 1.29.0 serialize different metadata fields. Compare every
    # field against a measured baseline, without stripping newer fields.
    baselines = json.loads(Path(__file__).with_name("mcp_contract_404.json").read_text())
    actual = _compatibility(_served_tools())
    assert actual in baselines.values(), "MCP contract differs from the measured serializer baselines"


@pytest.mark.parametrize("size", [UPDATE_BUDGET + 1, HOST_LIMIT, HOST_LIMIT + 100])
def test_length_gate_rejects_oversized_served_description(monkeypatch, size):
    tool = server.mcp._tool_manager.get_tool("update_context")
    monkeypatch.setattr(tool, "description", "x" * size)
    with pytest.raises(AssertionError, match=f"update_context: {size} chars"):
        _assert_lengths(_served_tools())


def test_length_gate_rejects_empty_discovery():
    with pytest.raises(AssertionError, match="nonempty"):
        _assert_lengths([])


@pytest.mark.parametrize("concept", CONTRACT)
def test_visibility_gate_rejects_removed_clause(monkeypatch, concept):
    tool = server.mcp._tool_manager.get_tool("update_context")
    description = " ".join(tool.description.split())
    monkeypatch.setattr(tool, "description", description.replace(CONTRACT[concept], "", 1))
    with pytest.raises(AssertionError, match=f"missing visible {concept} clause"):
        _assert_visible_contract(next(t["description"] for t in _served_tools()
                                      if t["name"] == "update_context"))


def test_visibility_gate_rejects_correction_clause_hidden_beyond_boundary(monkeypatch):
    tool = server.mcp._tool_manager.get_tool("update_context")
    description = " ".join(tool.description.split())
    clause = CONTRACT["correction_failure"]
    hidden = description.replace(clause, "", 1).ljust(HOST_LIMIT) + clause
    monkeypatch.setattr(tool, "description", hidden)
    with pytest.raises(AssertionError, match="missing visible correction_failure clause"):
        _assert_visible_contract(next(t["description"] for t in _served_tools()
                                      if t["name"] == "update_context"))
