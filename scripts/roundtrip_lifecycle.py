"""Live lifecycle round-trip: the real `RemoteStore` against a running Contexer Teams server.

This is the EXECUTABLE FORM of the `_WIRE_LIFECYCLE` validation checklist in
`contexer/remote.py`. That comment tells whoever touches the gate to repeat the identical live
validation; this is the thing to repeat, so it is committed rather than retyped from the prose.
Twenty-five checks over eleven scenarios: capability discovery, an active push and its
`get_context` readback, a retirement, a replayed identical event (no duplicate row, no timestamp
churn), a late OLDER event recorded as history without reversing the projection, a newer restore,
the whole sequence again through the batch tool, `retirementReasons=false` sending no prose, the
legacy payload an old server would receive, the invalid-params fallback (base decision syncs, the
refused delta stays pending), and a lead-approved team copy left untouched by a personal
retirement.

It is a MANUAL gate, not a test: it needs a server, a database and a token, so it is deliberately
outside `tests/` and never collected by pytest. The committed automatic half is
`tests/fixtures/lifecycle-contract.v1.json`, which pins the payload shapes byte for byte; this
script is what proves a real server accepts them.

Prerequisites (all four, or it fails at the first check):

1. A contexer-teams checkout whose migrations are applied, including `0063` - the migration that
   creates `decision_lifecycle_events` and the three nullable columns on `decisions`.
2. Postgres reachable, running in the `contexer-teams-postgres-1` container: `sql()` shells out
   through `docker exec ... psql` because half of what this validates is what the SERVER STORED,
   which no client call can answer. Change `PSQL` if your container or database is named
   differently.
3. The MCP server running against that database on `ENDPOINT` (the default is port 8099, chosen
   so it cannot collide with a real local install):

       DATABASE_URL='postgresql://contexer:contexer@localhost:5432/contexer_teams' \\
       MCP_PORT=8099 MCP_ISSUER_URL=http://localhost:8099 \\
       WEB_BASE_URL=http://localhost:3000 \\
       npx tsx --import ./src/telemetry.ts src/index.ts   # from apps/mcp-server

4. A bearer token for a real user, minted in the teams repo and passed as the one argument:

       npx tsx scripts/mint-token.ts --user-id <uuid>

Run it from this repo, and never paste the token into a committed file:

    uv run python scripts/roundtrip_lifecycle.py "$CONTEXER_TEAMS_TOKEN"

Exit status is 0 only when every check passed. It writes real rows under the token's own account
(decision ids are uuid-suffixed per run, so repeated runs do not collide), including one directly
inserted team-approved row for scenario 11 - point it at a throwaway database, not at production.
"""
import asyncio
import json
import subprocess
import sys
import uuid

import contexer.remote as remote
from contexer.remote import DecisionLifecycleCapabilities, RemoteStore

ENDPOINT = "http://localhost:8099/mcp"
PSQL = ["docker", "exec", "-i", "contexer-teams-postgres-1", "psql", "-U", "contexer",
        "-d", "contexer_teams", "-t", "-A", "-F", "|", "-c"]

LOG: list[tuple[str, bool, str]] = []


def sql(query: str) -> list[str]:
    """One psql query, as a list of `|`-joined rows. The server's own storage is half the
    contract: a client that pushed successfully proves nothing about what landed."""
    out = subprocess.run([*PSQL, query], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.strip().splitlines() if line]


def check(name: str, ok, detail: str = "") -> None:
    LOG.append((name, bool(ok), str(detail)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  :: {detail}" if detail else ""))


def main(token: str) -> int:
    assert remote._WIRE_LIFECYCLE is True, "the shipped gate must be open"
    dec = f"rt-{uuid.uuid4().hex[:8]}"
    rs = RemoteStore(ENDPOINT, token)

    # 0. capability discovery against the real server
    caps = rs.get_capabilities().decision_lifecycle
    check("capability advertised", caps == DecisionLifecycleCapabilities(
        version=1, revisions=True, tombstones=True, retirement_reasons=True), repr(caps))

    base = dict(type="constraint", content="never commit directly to main",
                repo="github.com/a/b", decision_id=dec)

    # 1. push an active personal decision
    srv = rs.push_decision(**base, revision_id="rev-1")
    check("1. active decision pushed", bool(srv), srv)
    row = sql("select id, current_oss_revision_id, deleted_at from decisions "
              f"where decision_id='{dec}'")
    row_id = row[0].split("|")[0]
    check("1. revision identity persisted", row[0].split("|")[1] == "rev-1", row[0])

    # 2. read it back through get_context
    check("2. visible in get_context", row_id in [d.id for d in rs.get_context().decisions])

    # 3. push a retired event
    retired = {"event_id": f"{dec}-ev1", "kind": "retired",
               "occurred_at": "2026-08-01T00:00:00+00:00", "actor": "human",
               "reason": "the queue moved to Kafka", "revision_id": "rev-3"}
    rs.push_decision(**base, revision_id="rev-3", lifecycle=[retired])
    check("3. retired: gone from live context",
          row_id not in [d.id for d in rs.get_context().decisions])
    hist = sql("select oss_event_id, kind, reason from decision_lifecycle_events "
               f"where decision_id='{row_id}' order by occurred_at")
    check("3. retired: appears in explicit history",
          len(hist) == 1 and hist[0].endswith("the queue moved to Kafka"), str(hist))
    before = sql(f"select deleted_at, lifecycle_event_key from decisions where id='{row_id}'")[0]

    # 4. replay the identical event
    rs.push_decision(**base, revision_id="rev-3", lifecycle=[retired])
    hist = sql(f"select oss_event_id from decision_lifecycle_events where decision_id='{row_id}'")
    after = sql(f"select deleted_at, lifecycle_event_key from decisions where id='{row_id}'")[0]
    check("4. replay: no duplicate row", len(hist) == 1, str(hist))
    check("4. replay: no timestamp churn", before == after, f"{before} -> {after}")

    # 5. a late OLDER restored event
    late = {"event_id": f"{dec}-ev0", "kind": "restored",
            "occurred_at": "2026-07-01T00:00:00+00:00", "actor": "human",
            "reason": "an older restore that arrived late"}
    rs.push_decision(**base, revision_id="rev-3", lifecycle=[late])
    after_late = sql(f"select deleted_at, lifecycle_event_key from decisions where id='{row_id}'")[0]
    hist = sql(f"select oss_event_id from decision_lifecycle_events where decision_id='{row_id}'")
    check("5. late older event: recorded as history", len(hist) == 2, str(hist))
    check("5. late older event: projection unchanged", after_late == after,
          f"{after} -> {after_late}")
    check("5. late older event: still absent from live context",
          row_id not in [d.id for d in rs.get_context().decisions])

    # 6. a NEWER restored event
    newer = {"event_id": f"{dec}-ev2", "kind": "restored",
             "occurred_at": "2026-08-02T00:00:00+00:00", "actor": "human",
             "reason": "the migration was reverted"}
    rs.push_decision(**base, revision_id="rev-4", lifecycle=[newer])
    check("6. newer restore: decision returns to live context",
          row_id in [d.id for d in rs.get_context().decisions])

    # 7. the same sequence through push_decisions
    bdec = f"{dec}-batch"
    batch_row = {**base, "decision_id": bdec,
                 "content": "always run migrations in a transaction", "revision_id": "rev-1",
                 "lifecycle": [{**retired, "event_id": f"{bdec}-ev1"}]}
    saved, skipped = rs.push_decisions([batch_row])
    check("7. batch: saved with lifecycle", len(saved) == 1 and skipped == [],
          f"{saved} {skipped}")
    brow = sql(f"select id, deleted_at from decisions where decision_id='{bdec}'")[0]
    brow_id = brow.split("|")[0]
    check("7. batch: tombstoned by its event", brow.split("|")[1] != "", brow)
    rs.push_decisions([batch_row])
    bhist = sql("select oss_event_id from decision_lifecycle_events "
                f"where decision_id='{brow_id}'")
    check("7. batch replay: no duplicate row", len(bhist) == 1, str(bhist))

    # 8. retirementReasons=false -> no reason egresses
    ndec = f"{dec}-noreason"
    args = remote._wire_args(
        type="constraint", content="never log a bearer token", repo="github.com/a/b",
        decision_id=ndec, revision_id="rev-1",
        lifecycle=[{**retired, "event_id": f"{ndec}-ev1", "reason": "REASONSHOULDNOTEGRESS"}],
        lifecycle_caps=DecisionLifecycleCapabilities(
            version=1, revisions=True, tombstones=True, retirement_reasons=False))
    check("8. retirementReasons=false: no reason on the wire",
          "reason" not in args["lifecycle"][0] and "REASONSHOULDNOTEGRESS" not in json.dumps(args),
          json.dumps(args["lifecycle"]))
    asyncio.run(rs._ainvoke("push_decision", args))
    nrow = sql(f"select id from decisions where decision_id='{ndec}'")[0]
    nres = sql("select coalesce(reason,'<null>') from decision_lifecycle_events "
               f"where decision_id='{nrow}'")
    check("8. retirementReasons=false: server stored no reason", nres == ["<null>"], str(nres))

    # 9. the legacy shape an OLD (non-advertising) server would receive, live
    ldec = f"{dec}-legacy"
    legacy = remote._wire_args(type="constraint", content="never commit directly to main",
                               repo="github.com/a/b", decision_id=ldec,
                               revision_id="rev-9", lifecycle=[retired], lifecycle_caps=None)
    check("9. legacy payload carries no optional fields",
          "lifecycle" not in legacy and "revision_id" not in legacy, json.dumps(legacy))
    asyncio.run(rs._ainvoke("push_decision", legacy))
    lrow = sql("select id, coalesce(current_oss_revision_id,'<null>'), "
               f"coalesce(deleted_at::text,'<null>') from decisions where decision_id='{ldec}'")[0]
    lhist = sql("select oss_event_id from decision_lifecycle_events "
                f"where decision_id='{lrow.split('|')[0]}'")
    check("9. legacy payload accepted, nothing lifecycle stored",
          lrow.split("|")[1] == "<null>" and lrow.split("|")[2] == "<null>" and lhist == [],
          f"{lrow} {lhist}")

    # 10. an advertised capability the server then refuses: base syncs, lifecycle stays pending
    fdec = f"{dec}-fallback"
    rs2 = RemoteStore(ENDPOINT, token)
    saved_kinds = remote._WIRE_LIFECYCLE_KINDS
    remote._WIRE_LIFECYCLE_KINDS = (*saved_kinds, "not_a_real_kind")
    try:
        sid = rs2.push_decision(
            type="constraint", content="never disable the guard", repo="github.com/a/b",
            decision_id=fdec, revision_id="rev-1",
            lifecycle=[{**retired, "event_id": f"{fdec}-ev1", "kind": "not_a_real_kind"}])
    finally:
        remote._WIRE_LIFECYCLE_KINDS = saved_kinds
    check("10. fallback: base decision still synced", bool(sid), sid)
    check("10. fallback: lifecycle recorded as blocked",
          [b["decision_id"] for b in rs2.lifecycle_blocked] == [fdec], str(rs2.lifecycle_blocked))
    check("10. fallback: capability disabled on this store", rs2._lifecycle_caps is None)
    frow = sql(f"select id from decisions where decision_id='{fdec}'")
    fhist = sql("select oss_event_id from decision_lifecycle_events "
                f"where decision_id='{frow[0]}'") if frow else ["?"]
    check("10. fallback: base row exists, no lifecycle row", len(frow) == 1 and fhist == [],
          f"{frow} {fhist}")

    # 11. a team-approved copy is never mutated by a personal retirement
    team_row = sql(
        "insert into decisions (team_id, author_user_id, type, content, state, approved_by, "
        "approved_at) select t.id, m.user_id, 'constraint', 'never commit directly to main', "
        "'team_approved', m.user_id, now() from teams t join memberships m "
        "on m.team_id = t.id where t.kind='team' limit 1 returning id")[0]
    tdec = f"{dec}-authority"
    rs.push_decision(type="constraint", content="never commit directly to main",
                     repo="github.com/a/b", decision_id=tdec, revision_id="rev-1",
                     lifecycle=[{**retired, "event_id": f"{tdec}-ev1"}])
    personal = sql("select coalesce(deleted_at::text,'<null>') from decisions "
                   f"where decision_id='{tdec}'")[0]
    team = sql("select coalesce(deleted_at::text,'<null>'), coalesce(lifecycle_event_key,'<null>') "
               f"from decisions where id='{team_row}'")[0]
    check("11. authority: personal copy retired", personal != "<null>", personal)
    check("11. authority: team copy untouched", team == "<null>|<null>", team)

    passed = sum(1 for _name, ok, _detail in LOG if ok)
    print(f"\n{passed}/{len(LOG)} checks passed")
    return 0 if passed == len(LOG) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
