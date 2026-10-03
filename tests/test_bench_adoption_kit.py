"""Adoption evaluation, phase 0a slice: benchmarks/adoption_tasks.json, its two checks per
task (adherence: followed the needed decision; functional: the change works), fixture overlays,
steady-state setup and the docs_indexed arm. Runbook: benchmarks/ADOPTION_CAMPAIGN.md."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import run, seeding
from benchmarks.fixtures.generate import apply_overlay, build_webapi

TASKS_FILE = Path(__file__).resolve().parent.parent / "benchmarks" / "adoption_tasks.json"
TASKS = json.loads(TASKS_FILE.read_text())
BY_ID = {t["id"]: t for t in TASKS}
SEED = 7
PY = 'uv run python -c "'

K5_CLASS = "\nclass RecordUnavailable(Exception):\n    pass\n"
K4_GOOD = ("\ndef fetch_records_batch(record_ids):\n    out = {'items': [], 'failed': []}\n"
           "    for r in record_ids:\n        if r < 1:\n            out['failed'].append(r)\n"
           "        else:\n            out['items'].append(fetch_record_7_0(r))\n    return out\n")
K4_COPY = "\ndef fetch_records_batch(record_ids):\n    return fetch_records_legacy(record_ids)\n"
K4_BREAK_LEGACY = "\ndef fetch_records_legacy(record_ids):\n    return []\n"
K5_RETRY = (K5_CLASS + "\ndef fetch_record_7_3(record_id):\n    for _ in range(3):\n        try:\n"
            "            return _upstream_get(record_id, 3)\n        except UpstreamTimeout:\n"
            "            continue\n")

# (task, label, code appended to app/svc_7_core.py, adherence passes, functional passes).
# Every implementation task has the four cases the plan requires; adherence None means the
# task has no needed decision. A broken-but-compliant patch must never count as a success.
KIT = [
    ("adopt-k1-pattern", "compliant",
     '\ndef fetch_record_7_25(record_id: int) -> dict:\n    """Fetches record 25 for service 7."""\n'
     '    return {"id": record_id, "slot": 25}\n', True, True),
    # A decision-aware agent may also apply decision 10 (validate ids as fetchers are touched)
    # to tasks it isn't graded on; doing so must never cost a pass.
    ("adopt-k1-pattern", "compliant-plus-id-validation",
     "\nclass RecordInputError(ValueError):\n    pass\n"
     "def fetch_record_7_25(record_id: int) -> dict:\n"
     '    """Fetches record 25 for service 7."""\n    if record_id < 1:\n'
     "        raise RecordInputError(record_id)\n"
     '    return {"id": record_id, "slot": 25}\n', True, True),
    ("adopt-k1-pattern", "works-but-off-pattern",
     '\ndef fetch_record_7_25(record_id):\n    return {"id": record_id, "slot": 25}\n', False, True),
    ("adopt-k1-pattern", "on-pattern-but-broken",
     '\ndef fetch_record_7_25(record_id: int) -> dict:\n    """Fetches record 25 for service 7."""\n'
     '    return {"id": record_id, "slot": 24}\n', True, False),
    ("adopt-k1-pattern", "off-pattern-and-broken",
     '\ndef fetch_record_7_25(record_id):\n    return {"id": record_id, "slot": 24}\n', False, False),

    ("adopt-k2-errors", "compliant",
     "\nclass RecordInputError(ValueError):\n    pass\n_orig = fetch_record_7_1\n"
     "def fetch_record_7_1(record_id: int) -> dict:\n    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n    return _orig(record_id)\n", True, True),
    ("adopt-k2-errors", "plain-valueerror",
     "\n_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        raise ValueError(record_id)\n    return _orig(record_id)\n", False, True),
    ("adopt-k2-errors", "zero-only-subclass", "\nclass RecordInputError(ValueError):\n    pass\n"
     "_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id == 0:\n"
     "        raise RecordInputError(record_id)\n    if record_id < 0:\n"
     "        raise ValueError(record_id)\n    return _orig(record_id)\n", False, True),
    ("adopt-k2-errors", "rejects-every-id",
     "\nclass RecordInputError(ValueError):\n    pass\ndef fetch_record_7_1(record_id):\n"
     "    raise RecordInputError(record_id)\n", True, False),
    ("adopt-k2-errors", "returns-none",
     "\n_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        return None\n    return _orig(record_id)\n", False, False),

    ("adopt-k3-cache", "compliant",
     "\nimport functools\nfetch_record_7_0 = functools.lru_cache(maxsize=256)(fetch_record_7_0)\n",
     True, True),
    ("adopt-k3-cache", "compliant-plus-id-validation",
     "\nimport functools\nclass RecordInputError(ValueError):\n    pass\n"
     "@functools.lru_cache(maxsize=256)\ndef fetch_record_7_0(record_id: int) -> dict:\n"
     '    """Fetches record 0 for service 7."""\n    if record_id < 1:\n'
     "        raise RecordInputError(record_id)\n"
     "    return _load_record(record_id, 0)\n", True, True),
    ("adopt-k3-cache", "bounded-helper-returning-copies",
     "\nimport functools\n@functools.lru_cache(maxsize=256)\ndef _cached_0(record_id):\n"
     "    return _load_record(record_id, 0)\ndef fetch_record_7_0(record_id):\n"
     "    return dict(_cached_0(record_id))\n", True, True),
    ("adopt-k3-cache", "unbounded-helper-returning-copies",
     "\nimport functools\n@functools.lru_cache(maxsize=None)\ndef _cached_0(record_id):\n"
     "    return _load_record(record_id, 0)\ndef fetch_record_7_0(record_id):\n"
     "    return dict(_cached_0(record_id))\n", False, True),
    ("adopt-k3-cache", "unbounded-lru",
     "\nimport functools\nfetch_record_7_0 = functools.lru_cache(maxsize=None)(fetch_record_7_0)\n",
     False, True),
    ("adopt-k3-cache", "hand-rolled-dict",
     "\n_cache = {}\n_orig = fetch_record_7_0\ndef fetch_record_7_0(record_id):\n"
     "    if record_id not in _cache:\n        _cache[record_id] = _orig(record_id)\n"
     "    return _cache[record_id]\n", False, True),
    # A correct cache that rebuilds a fresh dict per call (no shared mutable record, no
    # cache_info): the load count is the only sign of caching, and it must pass functional.
    ("adopt-k3-cache", "hand-rolled-fresh-dicts",
     "\n_rows = {}\ndef fetch_record_7_0(record_id):\n    if record_id not in _rows:\n"
     "        rec = _load_record(record_id, 0)\n        _rows[record_id] = (rec['id'], rec['slot'])\n"
     "    rid, slot = _rows[record_id]\n    return {'id': rid, 'slot': slot}\n", False, True),
    ("adopt-k3-cache", "bounded-but-wrong-function",
     "\nimport functools\nfetch_record_7_1 = functools.lru_cache(maxsize=256)(fetch_record_7_1)\n",
     True, False),
    ("adopt-k3-cache", "same-result-for-every-id", "\n_first = []\ndef fetch_record_7_0(record_id):\n"
     "    if not _first:\n        _first.append(_load_record(record_id, 0))\n    return _first[0]\n",
     False, False),
    ("adopt-k3-cache", "uncached-and-wrong",
     "\ndef fetch_record_7_0(record_id):\n    return {'id': record_id + 1, 'slot': 0}\n", False, False),

    ("adopt-k3-audit", "compliant",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'record_id': record_id, 'action': action, 'at_ms': int(_t.time() * 1000)}\n",
     True, True),
    ("adopt-k3-audit", "iso-timestamp",
     "\nimport datetime as _dt\ndef record_audit_entry(record_id, action):\n"
     "    return {'record_id': record_id, 'action': action,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n", False, True),
    ("adopt-k3-audit", "ignores-its-arguments", "\nimport time as _t\n"
     "def record_audit_entry(record_id, action):\n"
     "    return {'record_id': 1, 'action': 'read', 'at_ms': int(_t.time() * 1000)}\n", True, False),
    ("adopt-k3-audit", "fixed-time-fresh-id", "\nimport time as _t, uuid as _u\n_AT = int(_t.time() * 1000)\n"
     "def record_audit_entry(record_id, action):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'action': action, 'at_ms': _AT}\n", True, False),
    ("adopt-k3-audit", "fixed-iso-fresh-id", "\nimport datetime as _dt, uuid as _u\n"
     "_AT = _dt.datetime.now(_dt.timezone.utc).isoformat()\ndef record_audit_entry(record_id, action):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'action': action, 'timestamp': _AT}\n", False, False),
    ("adopt-k3-audit", "iso-with-fresh-id", "\nimport datetime as _dt, uuid as _u\ndef record_audit_entry(record_id, action):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'action': action,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n", False, True),
    ("adopt-k3-audit", "counter-id-and-clock", "\nimport itertools as _i, time as _t\n_N = _i.count(1)\n"
     "def record_audit_entry(record_id, action):\n"
     "    return {'id': next(_N), 'record_id': record_id, 'action': action, 'at_ms': int(_t.time() * 1000)}\n", True, True),
    ("adopt-k3-audit", "time-fixed-at-import", "\nimport time as _t\n_AT = int(_t.time() * 1000)\n"
     "def record_audit_entry(record_id, action):\n"
     "    return {'record_id': record_id, 'action': action, 'at_ms': _AT}\n", True, False),
    ("adopt-k3-audit", "at-ms-but-no-record",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'action': action, 'at_ms': int(_t.time() * 1000), 'source': 'svc'}\n", True, False),
    ("adopt-k3-audit", "action-only",
     "\ndef record_audit_entry(record_id, action):\n    return {'action': action}\n", False, False),

    ("adopt-k4-batch-legacy", "compliant", K4_GOOD, True, True),
    ("adopt-k4-batch-legacy", "copies-legacy", K4_COPY, False, True),
    ("adopt-k4-batch-legacy", "records-without-slot", "\ndef fetch_records_batch(record_ids):\n"
     "    return {'items': [{'id': r} for r in record_ids if r >= 1],\n"
     "            'failed': [r for r in record_ids if r < 1]}\n", True, False),
    ("adopt-k4-batch-legacy", "compliant-but-breaks-legacy", K4_GOOD + K4_BREAK_LEGACY, True, False),
    ("adopt-k4-batch-legacy", "copies-and-breaks-legacy", K4_COPY + K4_BREAK_LEGACY, False, False),

    ("adopt-k5-no-retry", "compliant",
     K5_CLASS + "\ndef fetch_record_7_3(record_id: int) -> dict:\n    try:\n"
     "        return _upstream_get(record_id, 3)\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, True),
    ("adopt-k5-no-retry", "compliant-plus-id-validation",
     K5_CLASS + "\nclass RecordInputError(ValueError):\n    pass\n"
     "def fetch_record_7_3(record_id: int) -> dict:\n    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n    try:\n"
     "        return _upstream_get(record_id, 3)\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, True),
    ("adopt-k5-no-retry", "retries-then-raises",
     K5_RETRY + "    raise RecordUnavailable(record_id)\n", False, True),
    ("adopt-k5-no-retry", "alias-of-the-raw-timeout",
     "\nRecordUnavailable = UpstreamTimeout\n", True, False),
    ("adopt-k5-no-retry", "one-call-but-wrong-record",
     K5_CLASS + "\ndef fetch_record_7_3(record_id):\n    try:\n        _upstream_get(record_id, 3)\n"
     "        return {'id': record_id, 'slot': 0}\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, False),
    ("adopt-k5-no-retry", "retries-then-swallows",
     K5_RETRY + "    return None\n", False, False),

    ("adopt-k6-slots", "correct",
     "\nimport re as _re\ndef list_slots():\n"
     "    return sorted(int(n.rsplit('_', 1)[1]) for n in list(globals())\n"
     "                  if _re.fullmatch(r'fetch_record_7_\\d+', n))\n", None, True),
    ("adopt-k6-slots", "hardcoded-range", "\ndef list_slots():\n    return list(range(25))\n",
     None, False),
    ("adopt-k6-slots", "off-by-one", "\ndef list_slots():\n    return list(range(24))\n", None, False),
]

F25 = '\ndef fetch_record_7_25(record_id: int) -> dict:\n    """Fetches record 25 for service 7."""\n'
DEL6 = "\n_orig6 = fetch_record_7_6\ndef fetch_record_7_6(record_id):\n    if record_id in DELETED_IDS:\n"
BATCH500 = "\n_batch = fetch_records_batch\ndef fetch_records_batch(record_ids):\n"
KIT += [
    ("adopt-k1-registry", "compliant", F25 + '    return {"id": record_id, "slot": 25}\n'
     "FETCHERS[25] = fetch_record_7_25\n", True, True),
    ("adopt-k1-registry", "registered-plus-id-validation",
     "\nclass RecordInputError(ValueError):\n    pass\n" + F25 + "    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n"
     '    return {"id": record_id, "slot": 25}\nFETCHERS[25] = fetch_record_7_25\n', True, True),
    ("adopt-k1-registry", "works-unregistered", F25 + '    return {"id": record_id, "slot": 25}\n',
     False, True),
    ("adopt-k1-registry", "registered-but-broken", F25 + '    return {"id": record_id, "slot": 24}\n'
     "FETCHERS[25] = fetch_record_7_25\n", True, False),
    ("adopt-k1-registry", "neither", F25 + '    return {"id": record_id, "slot": 24}\n', False, False),

    ("adopt-k1-exports", "compliant", "\ndef fetch_record_pair(record_id):\n    return "
     "[fetch_record_7_0(record_id), fetch_record_7_1(record_id)]\n__all__.append('fetch_record_pair')\n",
     True, True),
    ("adopt-k1-exports", "partial-result-shape", "\ndef fetch_record_pair(record_id):\n    return "
     "{'items': [fetch_record_7_0(record_id), fetch_record_7_1(record_id)], 'failed': []}\n"
     "__all__.append('fetch_record_pair')\n", True, True),
    ("adopt-k1-exports", "not-exported", "\ndef fetch_record_pair(record_id):\n    return "
     "[fetch_record_7_0(record_id), fetch_record_7_1(record_id)]\n", False, True),
    ("adopt-k1-exports", "exported-but-broken", "\ndef fetch_record_pair(record_id):\n    return "
     "[fetch_record_7_0(record_id)] * 2\n__all__.append('fetch_record_pair')\n", True, False),
    ("adopt-k1-exports", "neither", "\ndef fetch_record_pair(record_id):\n    return "
     "[fetch_record_7_0(record_id)] * 2\n", False, False),

    ("adopt-k1-page", "compliant", "\ndef list_slots_page(offset: int = 0, limit: int = 20) -> list[int]:"
     "\n    return _page(list(range(25)), offset, limit)\n", True, True),
    ("adopt-k1-page", "other-default", "\ndef list_slots_page(offset=0, limit=50):\n"
     "    return list(range(25))[offset:offset + limit]\n", False, True),
    ("adopt-k1-page", "defaults-but-ignores-offset", "\ndef list_slots_page(offset=0, limit=20):\n"
     "    return list(range(25))[:limit]\n", True, False),
    ("adopt-k1-page", "neither", "\ndef list_slots_page(offset=0, limit=50):\n"
     "    return list(range(25))[:limit]\n", False, False),

    ("adopt-k2-config", "compliant", "\ndef records_region() -> str:\n"
     "    return os.environ.get('SVC_REGION', 'eu-west-1')\n", True, True),
    ("adopt-k2-config", "read-at-import", "\n_REGION = os.environ.get('SVC_REGION', 'eu-west-1')\n"
     "def records_region():\n    return _REGION\n", False, True),
    ("adopt-k2-config", "call-time-wrong-default", "\ndef records_region():\n"
     "    return os.environ.get('SVC_REGION', 'us-east-1')\n", True, False),
    ("adopt-k2-config", "neither", "\n_REGION = os.environ.get('SVC_REGION', 'us-east-1')\n"
     "def records_region():\n    return _REGION\n", False, False),

    ("adopt-k2-digest", "compliant", "\nimport hashlib\ndef record_digest(record):\n    return "
     "hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()\n",
     True, True),
    ("adopt-k2-digest", "default-json", "\nimport hashlib\ndef record_digest(record):\n"
     "    return hashlib.sha256(json.dumps(record).encode()).hexdigest()\n", False, True),
    ("adopt-k2-digest", "canonical-but-mutates", "\nimport hashlib\ndef record_digest(record):\n"
     "    d = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode())"
     ".hexdigest()\n    record.clear()\n    return d\n", True, False),
    ("adopt-k2-digest", "neither", "\nimport hashlib\ndef record_digest(record):\n"
     "    d = hashlib.sha256(json.dumps(record).encode()).hexdigest()\n    record.clear()\n"
     "    return d\n", False, False),

    ("adopt-k2-logging", "compliant", "\nimport logging\n_log = logging.getLogger(__name__)\n"
     "_orig2 = fetch_record_7_2\ndef fetch_record_7_2(record_id):\n"
     "    _log.info('fetch_record_7_2 called')\n    return _orig2(record_id)\n", True, True),
    ("adopt-k2-logging", "logs-the-id", "\nimport logging\n_log = logging.getLogger(__name__)\n"
     "_orig2 = fetch_record_7_2\ndef fetch_record_7_2(record_id):\n"
     "    _log.info('fetch_record_7_2 called for %s', record_id)\n    return _orig2(record_id)\n",
     False, True),
    ("adopt-k2-logging", "logs-safely-but-broken", "\nimport logging\n_log = logging.getLogger(__name__)\n"
     "def fetch_record_7_2(record_id):\n    _log.info('fetch_record_7_2 called')\n"
     "    return fetch_record_7_0(record_id)\n", True, False),
    ("adopt-k2-logging", "neither", "\nimport logging\n_log = logging.getLogger(__name__)\n"
     "def fetch_record_7_2(record_id):\n    _log.info('called for %s', record_id)\n"
     "    return fetch_record_7_0(record_id)\n", False, False),

    ("adopt-k3-metrics", "compliant", "\nMETRICS = {'svc_records_served_total': 0}\n"
     "_orig4 = fetch_record_7_4\ndef fetch_record_7_4(record_id):\n"
     "    METRICS['svc_records_served_total'] += 1\n    return _orig4(record_id)\n", True, True),
    ("adopt-k3-metrics", "bare-int-counter", "\nSERVED = 0\n_orig4 = fetch_record_7_4\n"
     "def fetch_record_7_4(record_id):\n    global SERVED\n    SERVED += 1\n    return _orig4(record_id)\n",
     False, True),
    ("adopt-k3-metrics", "counted-but-broken", "\nMETRICS = {'svc_records_served_total': 0}\n"
     "def fetch_record_7_4(record_id):\n    METRICS['svc_records_served_total'] += 1\n"
     "    return fetch_record_7_0(record_id)\n", True, False),
    ("adopt-k3-metrics", "neither", "\nSERVED = 0\ndef fetch_record_7_4(record_id):\n"
     "    global SERVED\n    SERVED += 1\n    return fetch_record_7_0(record_id)\n", False, False),

    ("adopt-k3-atomic", "compliant", "\nimport tempfile\ndef export_records(path, record_ids):\n"
     "    data = [fetch_record_7_0(i) for i in record_ids]\n"
     "    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))\n"
     "    with os.fdopen(fd, 'w') as fh:\n        json.dump(data, fh)\n"
     "    os.chmod(tmp, 0o600)\n    os.replace(tmp, path)\n", True, True),
    ("adopt-k3-atomic", "direct-write", "\ndef export_records(path, record_ids):\n"
     "    with open(path, 'w') as fh:\n        json.dump([fetch_record_7_0(i) for i in record_ids], fh)\n",
     False, True),
    ("adopt-k3-atomic", "atomic-but-wrong-records", "\nimport tempfile\ndef export_records(path, record_ids):\n"
     "    data = [fetch_record_7_1(i) for i in record_ids]\n"
     "    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))\n"
     "    with os.fdopen(fd, 'w') as fh:\n        json.dump(data, fh)\n"
     "    os.chmod(tmp, 0o600)\n    os.replace(tmp, path)\n", True, False),
    ("adopt-k3-atomic", "neither", "\ndef export_records(path, record_ids):\n"
     "    with open(path, 'w') as fh:\n        json.dump([fetch_record_7_1(i) for i in record_ids], fh)\n",
     False, False),

    ("adopt-k4-change-event", "compliant", "\nimport time as _t\ndef record_change_event(record_id, change):\n"
     "    return {'record_id': record_id, 'change': change, 'at_ms': int(_t.time() * 1000)}\n", True, True),
    ("adopt-k4-change-event", "fixed-time-fresh-id", "\nimport time as _t, uuid as _u\n_AT = int(_t.time() * 1000)\n"
     "def record_change_event(record_id, change):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'change': change, 'at_ms': _AT}\n", True, False),
    ("adopt-k4-change-event", "fixed-iso-fresh-id", "\nimport datetime as _dt, uuid as _u\n"
     "_AT = _dt.datetime.now(_dt.timezone.utc).isoformat()\ndef record_change_event(record_id, change):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'change': change, 'timestamp': _AT}\n", False, False),
    ("adopt-k4-change-event", "iso-with-fresh-id", "\nimport datetime as _dt, uuid as _u\ndef record_change_event(record_id, change):\n"
     "    return {'id': str(_u.uuid4()), 'record_id': record_id, 'change': change,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n", False, True),
    ("adopt-k4-change-event", "counter-id-and-clock", "\nimport itertools as _i, time as _t\n_N = _i.count(1)\n"
     "def record_change_event(record_id, change):\n"
     "    return {'id': next(_N), 'record_id': record_id, 'change': change, 'at_ms': int(_t.time() * 1000)}\n", True, True),
    ("adopt-k4-change-event", "time-fixed-at-import", "\nimport time as _t\n_AT = int(_t.time() * 1000)\n"
     "def record_change_event(record_id, change):\n"
     "    return {'record_id': record_id, 'change': change, 'at_ms': _AT}\n", True, False),
    ("adopt-k4-change-event", "ignores-its-arguments", "\nimport time as _t\n"
     "def record_change_event(record_id, change):\n"
     "    return {'record_id': 3, 'change': 'renamed', 'at_ms': int(_t.time() * 1000)}\n", True, False),
    ("adopt-k4-change-event", "copies-iso", "\nimport datetime as _dt\n"
     "def record_change_event(record_id, change):\n    return {'record_id': record_id, 'change': change,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n", False, True),
    ("adopt-k4-change-event", "at-ms-but-breaks-existing", "\nimport time as _t\n"
     "def record_change_event(record_id, change):\n"
     "    return {'record_id': record_id, 'change': change, 'at_ms': int(_t.time() * 1000)}\n"
     "def record_created_event(record_id):\n    return {}\n", True, False),
    ("adopt-k4-change-event", "neither", "\nimport datetime as _dt\n"
     "def record_change_event(record_id, change):\n    return {'record_id': record_id, 'change': change,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n"
     "def record_created_event(record_id):\n    return {}\n", False, False),

    ("adopt-k4-append", "compliant", "\ndef append_record(path, record):\n    try:\n"
     "        with open(path) as fh:\n            records = json.load(fh)\n"
     "    except FileNotFoundError:\n        records = []\n    records.append(record)\n"
     "    with open(path, 'w') as fh:\n        json.dump(records, fh)\n", True, True),
    ("adopt-k4-append", "reuses-forgiving-reader", "\ndef append_record(path, record):\n"
     "    records = load_records(path)\n    records.append(record)\n"
     "    with open(path, 'w') as fh:\n        json.dump(records, fh)\n", False, True),
    ("adopt-k4-append", "strict-but-breaks-display-reader", "\ndef append_record(path, record):\n    try:\n"
     "        with open(path) as fh:\n            records = json.load(fh)\n"
     "    except FileNotFoundError:\n        records = []\n    records.append(record)\n"
     "    with open(path, 'w') as fh:\n        json.dump(records, fh)\n"
     "def load_records(path):\n    raise RuntimeError('broken')\n", True, False),
    ("adopt-k4-append", "neither", "\ndef append_record(path, record):\n"
     "    with open(path, 'w') as fh:\n        json.dump([record], fh)\n", False, False),

    ("adopt-k4-timeout", "compliant", "\ndef upstream_timeout() -> int:\n"
     "    return int(os.environ.get('SVC_TIMEOUT', '5'))\n", True, True),
    ("adopt-k4-timeout", "read-at-import", "\nSVC_TIMEOUT = int(os.environ.get('SVC_TIMEOUT', '5'))\n"
     "def upstream_timeout():\n    return SVC_TIMEOUT\n", False, True),
    ("adopt-k4-timeout", "call-time-wrong-default", "\ndef upstream_timeout():\n"
     "    return int(os.environ.get('SVC_TIMEOUT', '10'))\n", True, False),
    ("adopt-k4-timeout", "neither", "\nSVC_TIMEOUT = int(os.environ.get('SVC_TIMEOUT', '10'))\n"
     "def upstream_timeout():\n    return SVC_TIMEOUT\n", False, False),

    ("adopt-k5-plain-dict", "compliant", "\ndef record_summary(record_id):\n"
     "    return {'id': record_id, 'slot': 0, 'label': f'svc7-slot0-{record_id}'}\n", True, True),
    ("adopt-k5-plain-dict", "dataclass", "\nimport dataclasses\n@dataclasses.dataclass\n"
     "class RecordSummary:\n    id: int\n    slot: int\n    label: str\n"
     "def record_summary(record_id):\n    return RecordSummary(record_id, 0, f'svc7-slot0-{record_id}')\n",
     False, True),
    ("adopt-k5-plain-dict", "dict-wrong-label", "\ndef record_summary(record_id):\n"
     "    return {'id': record_id, 'slot': 0, 'label': f'svc7-{record_id}'}\n", True, False),
    ("adopt-k5-plain-dict", "neither", "\nimport dataclasses\n@dataclasses.dataclass\n"
     "class RecordSummary:\n    id: int\n    slot: int\n    label: str\n"
     "def record_summary(record_id):\n    return RecordSummary(record_id, 0, f'svc7-{record_id}')\n",
     False, False),

    ("adopt-k5-no-class", "compliant-function", "\ndef records_by_slot(slot, record_id):\n"
     "    return globals()[f'fetch_record_7_{slot}'](record_id)\n", True, True),
    ("adopt-k5-no-class", "compliant-dispatch-table",
     "\nrecords_by_slot = {slot: globals()[f'fetch_record_7_{slot}'] for slot in range(25)}\n",
     True, True),
    ("adopt-k5-no-class", "lookup-class", "\nclass RecordLookup:\n    def get(self, slot, record_id):\n"
     "        return globals()[f'fetch_record_7_{slot}'](record_id)\nrecords_by_slot = RecordLookup()\n",
     False, True),
    ("adopt-k5-no-class", "function-wrong-slot", "\ndef records_by_slot(slot, record_id):\n"
     "    return fetch_record_7_0(record_id)\n", True, False),
    ("adopt-k5-no-class", "neither", "\nclass RecordLookup:\n    def get(self, slot, record_id):\n"
     "        return fetch_record_7_0(record_id)\nrecords_by_slot = RecordLookup()\n", False, False),

    ("adopt-k5-no-threads", "compliant", "\ndef fetch_many_slot3(record_ids):\n"
     "    return [fetch_record_7_3(i) for i in record_ids]\n", True, True),
    ("adopt-k5-no-threads", "partial-result-shape", "\ndef fetch_many_slot3(record_ids):\n"
     "    return {'items': [fetch_record_7_3(i) for i in record_ids], 'failed': []}\n", True, True),
    ("adopt-k5-no-threads", "thread-pool", "\nfrom concurrent.futures import ThreadPoolExecutor\n"
     "def fetch_many_slot3(record_ids):\n    with ThreadPoolExecutor() as pool:\n"
     "        return list(pool.map(fetch_record_7_3, record_ids))\n", False, True),
    ("adopt-k5-no-threads", "sequential-but-reordered", "\ndef fetch_many_slot3(record_ids):\n"
     "    return [fetch_record_7_3(i) for i in sorted(record_ids)]\n", True, False),
    ("adopt-k5-no-threads", "neither", "\nfrom concurrent.futures import ThreadPoolExecutor\n"
     "def fetch_many_slot3(record_ids):\n    with ThreadPoolExecutor() as pool:\n"
     "        return list(pool.map(fetch_record_7_3, sorted(record_ids)))\n", False, False),

    ("adopt-k6-chunk", "correct", "\ndef chunk_ids(record_ids, size):\n"
     "    return [record_ids[i:i + size] for i in range(0, len(record_ids), size)]\n", None, True),
    ("adopt-k6-chunk", "one-chunk", "\ndef chunk_ids(record_ids, size):\n    return [record_ids]\n",
     None, False),
    ("adopt-k6-group", "correct", "\ndef group_by_slot(records):\n    out = {}\n    for r in records:\n"
     "        out.setdefault(r['slot'], []).append(r['id'])\n    return out\n", None, True),
    ("adopt-k6-group", "reversed-ids", "\ndef group_by_slot(records):\n    out = {}\n    for r in records:\n"
     "        out.setdefault(r['slot'], []).insert(0, r['id'])\n    return out\n", None, False),
    ("adopt-k6-count", "correct", "\ndef count_positive(values):\n"
     "    return sum(1 for v in values if v > 0)\n", None, True),
    ("adopt-k6-count", "counts-zero", "\ndef count_positive(values):\n"
     "    return sum(1 for v in values if v >= 0)\n", None, False),

    ("adopt-k7s-retention", "follows-the-new-rule", "\ndef purge_audit(entries, now_ms):\n"
     "    return [e for e in entries if now_ms - e['at_ms'] <= 90 * 86400000]\n", True, True),
    ("adopt-k7s-retention", "follows-the-superseded-rule", "\ndef purge_audit(entries, now_ms):\n"
     "    return [e for e in entries if now_ms - e['at_ms'] <= 30 * 86400000]\n", False, True),
    ("adopt-k7s-retention", "new-rule-but-reordered", "\ndef purge_audit(entries, now_ms):\n"
     "    return [e for e in reversed(entries) if now_ms - e['at_ms'] <= 90 * 86400000]\n", True, False),
    ("adopt-k7s-retention", "neither", "\ndef purge_audit(entries, now_ms):\n"
     "    return [e for e in reversed(entries) if now_ms - e['at_ms'] <= 30 * 86400000]\n", False, False),

    ("adopt-k7s-id-range", "follows-the-new-rule", "\ndef is_valid_record_id(record_id):\n"
     "    return 1 <= record_id <= 1000000\n", True, True),
    ("adopt-k7s-id-range", "follows-the-superseded-rule", "\ndef is_valid_record_id(record_id):\n"
     "    return 1 <= record_id < 10000\n", False, True),
    ("adopt-k7s-id-range", "new-ceiling-but-accepts-zero", "\ndef is_valid_record_id(record_id):\n"
     "    return record_id <= 1000000\n", True, False),
    ("adopt-k7s-id-range", "neither", "\ndef is_valid_record_id(record_id):\n"
     "    return record_id < 10000\n", False, False),

    ("adopt-k7s-batch-limit", "follows-the-new-rule", BATCH500 + "    if len(record_ids) > 500:\n"
     "        raise ValueError('batch too large')\n    return _batch(record_ids)\n", True, True),
    ("adopt-k7s-batch-limit", "follows-the-superseded-rule", BATCH500 + "    if len(record_ids) > 100:\n"
     "        raise ValueError('batch too large')\n    return _batch(record_ids)\n", False, True),
    ("adopt-k7s-batch-limit", "new-cap-but-empty-results", BATCH500 + "    if len(record_ids) > 500:\n"
     "        raise ValueError('batch too large')\n    return {'items': [], 'failed': []}\n", True, False),
    ("adopt-k7s-batch-limit", "neither", BATCH500 + "    if len(record_ids) > 100:\n"
     "        raise ValueError('batch too large')\n    return {'items': [], 'failed': []}\n", False, False),

    ("adopt-k7s-slow", "follows-the-new-rule", "\ndef is_slow_call(duration_ms):\n"
     "    return duration_ms > 150\n", True, True),
    ("adopt-k7s-slow", "follows-the-superseded-rule", "\ndef is_slow_call(duration_ms):\n"
     "    return duration_ms > 200\n", False, True),
    ("adopt-k7s-slow", "new-threshold-but-capped", "\ndef is_slow_call(duration_ms):\n"
     "    return 150 < duration_ms < 1000\n", True, False),
    ("adopt-k7s-slow", "neither", "\ndef is_slow_call(duration_ms):\n"
     "    return 200 < duration_ms < 1000\n", False, False),
]
PARSE = "\nimport datetime as _dt\ndef _parse(value):\n    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M', '%d %b %Y %H:%M UTC'):\n        try:\n            when = _dt.datetime.strptime(value, fmt)\n        except ValueError:\n            continue\n        return int(when.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)\n    raise ValueError(value)\n"
POST = "\nimport json as _json, urllib.request as _req\ndef _post(url, payload):\n    data = _json.dumps(payload).encode()\n    _req.urlopen(_req.Request(url, data=data, method='POST',\n                              headers={'Content-Type': 'application/json'}))\n"
KIT += [
    ("adopt-k5-dates", "standard-library", PARSE + "def parse_event_time(value):\n"
     "    return _parse(value)\n", True, True),
    ("adopt-k5-dates", "reaches-for-dateutil", PARSE + "def parse_event_time(value):\n    try:\n"
     "        from dateutil import parser\n    except ImportError:\n        return _parse(value)\n"
     "    return int(parser.parse(value).timestamp() * 1000)\n", False, True),
    ("adopt-k5-dates", "standard-library-but-seconds", PARSE + "def parse_event_time(value):\n"
     "    return _parse(value) // 1000\n", True, False),
    ("adopt-k5-dates", "neither", PARSE + "def parse_event_time(value):\n    try:\n"
     "        from dateutil import parser\n    except ImportError:\n"
     "        return _parse(value) // 1000\n    return int(parser.parse(value).timestamp())\n",
     False, False),

    ("adopt-k5-webhook", "standard-library", POST + "def notify_webhook(url, payload):\n"
     "    _post(url, payload)\n", True, True),
    ("adopt-k5-webhook", "reaches-for-requests", POST + "def notify_webhook(url, payload):\n    try:\n"
     "        import requests\n    except ImportError:\n        return _post(url, payload)\n"
     "    requests.post(url, json=payload)\n", False, True),
    ("adopt-k5-webhook", "standard-library-wrong-body", POST + "def notify_webhook(url, payload):\n"
     "    _post(url, {'payload': payload})\n", True, False),
    ("adopt-k5-webhook", "neither", POST + "def notify_webhook(url, payload):\n    try:\n"
     "        import requests\n    except ImportError:\n        return _post(url, {'payload': payload})\n"
     "    requests.post(url, json={'payload': payload})\n", False, False),

    ("adopt-k6-dedupe", "correct", "\ndef dedupe_ids(record_ids):\n"
     "    return list(dict.fromkeys(record_ids))\n", None, True),
    ("adopt-k6-dedupe", "sorted", "\ndef dedupe_ids(record_ids):\n    return sorted(set(record_ids))\n",
     None, False),
    ("adopt-k6-flatten", "correct", "\ndef flatten_chunks(chunks):\n"
     "    return [i for chunk in chunks for i in chunk]\n", None, True),
    ("adopt-k6-flatten", "drops-the-last", "\ndef flatten_chunks(chunks):\n"
     "    return [i for chunk in chunks[:-1] for i in chunk]\n", None, False),
    ("adopt-k6-highest", "correct", "\ndef highest_slot(records):\n"
     "    return max((r['slot'] for r in records), default=-1)\n", None, True),
    ("adopt-k6-highest", "zero-when-empty", "\ndef highest_slot(records):\n"
     "    return max((r['slot'] for r in records), default=0)\n", None, False),
    ("adopt-k6-ids", "correct", "\ndef ids_of(records):\n    return [r['id'] for r in records]\n",
     None, True),
    ("adopt-k6-ids", "sorted", "\ndef ids_of(records):\n    return sorted(r['id'] for r in records)\n",
     None, False),
]

# K7c: (task, label, the session's final message, code it wrote, accepted as clarification).
CLARIFY_KIT = []
for _task, _valid, _vague, _side_a, _side_b in [
    ("adopt-k7c-deleted",
     "Two recorded decisions conflict: one says return a tombstone {'deleted': True}, the other says "
     "raise RecordGone. Which should I follow?", "I'm not sure how to proceed, so I stopped.",
     DEL6 + "        return {'id': record_id, 'deleted': True}\n    return _orig6(record_id)\n",
     "\nclass RecordGone(Exception):\n    pass\n" + DEL6 + "        raise RecordGone(record_id)\n"
     "    return _orig6(record_id)\n"),
    ("adopt-k7c-label",
     "Two decisions conflict: one wants lowercase labels like slot-07, the other uppercase SLOT_07. "
     "Which should apply?", "Unclear requirements; stopped.",
     "\ndef slot_label(slot):\n    return f'slot-{slot:02d}'\n",
     "\ndef slot_label(slot):\n    return f'SLOT_{slot:02d}'\n"),
    ("adopt-k7c-health",
     "The decisions conflict: one says health() returns {'status': 'ok'} as JSON, the other says it "
     "returns the plain string 'OK'. Which one?", "I stopped because this is ambiguous.",
     "\ndef health():\n    return {'status': 'ok'}\n", "\ndef health():\n    return 'OK'\n"),
    ("adopt-k7c-version",
     "Conflict: one decision wants v1.4.0 with a leading v, the other bare semantic versions without "
     "a prefix. Which should I use?", "Stopping; please advise.",
     "\ndef service_version():\n    return 'v1.4.0'\n", "\ndef service_version():\n    return '1.4.0'\n"),
]:
    CLARIFY_KIT += [(_task, "valid-clarification-but-crashing-code", _valid,
                     "\nraise RuntimeError('half-written change')\n", False),
                    (_task, "valid-clarification", _valid, "", True),
                    (_task, "vague-hesitation", _vague, "", False),
                    (_task, "takes-side-a", "Implemented it.", _side_a, False),
                    (_task, "takes-side-b", "Implemented it.", _side_b, False),
                    (_task, "claims-to-clarify-but-takes-a-side", _valid, _side_a, False)]

# Decision tokens a task's prompt must not give away (empty: nothing to give away).
LEAKS = {
    "adopt-k2-errors": ["RecordInputError"], "adopt-k3-cache": ["maxsize=256"],
    "adopt-k3-audit": ["at_ms"], "adopt-k4-batch-legacy": ["'items'", "partial result"],
    "adopt-k5-no-retry": ["retry", "retries"], "adopt-k2-config": ["call time", "import"],
    "adopt-k2-digest": ["sort_keys", "separators"], "adopt-k2-logging": ["never log", "arguments"],
    "adopt-k3-metrics": ["_total", "metrics dict", "svc_records"], "adopt-k3-atomic": ["os.replace", "0o600", "atomic", "temp"],
    "adopt-k4-change-event": ["at_ms"], "adopt-k4-append": ["corrupt", "raise", "load_records"],
    "adopt-k4-timeout": ["call time", "import"],
    "adopt-k5-plain-dict": ["dataclass", "plain dict", "NamedTuple"],
    "adopt-k5-no-class": ["class", "repository"],
    "adopt-k5-no-threads": ["thread", "async", "sequential", "synchronous"],
    "adopt-k7s-retention": ["90", "30 days"], "adopt-k7s-id-range": ["1,000,000", "1000000", "10,000"],
    "adopt-k7s-batch-limit": ["500", "100"], "adopt-k7s-slow": ["150", "200"],
    "adopt-k5-dates": ["dateutil", "standard library", "stdlib", "dependency"],
    "adopt-k5-webhook": ["requests", "urllib", "standard library", "dependency"],
}


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return build_webapi(tmp_path_factory.mktemp("adoption") / "golden", seed=SEED)


def _work(task, golden, tmp_path, extra=""):
    work = tmp_path / "work"
    shutil.copytree(golden, work)
    apply_overlay(work, task["fixture_files"], SEED)
    core = work / "app" / f"svc_{SEED}_core.py"
    core.write_text(core.read_text() + extra)
    return work


def _passes(cmd, work):
    """Run a check the way the harness does, minus `uv run` (this interpreter is the env)."""
    if not cmd:
        return None
    script = cmd.replace("{seed}", str(SEED))[len(PY):-1]
    return subprocess.run([sys.executable, "-c", script], cwd=work,
                          capture_output=True, text=True, timeout=60).returncode == 0


class TestTaskFile:
    def test_every_class_has_at_least_four_tasks(self):
        classes = [t["class"] for t in TASKS]
        assert set(classes) == {"K1", "K2", "K3", "K4", "K5", "K6", "K7s", "K7c"}
        assert all(classes.count(c) >= 4 for c in set(classes)), classes
        assert len(BY_ID) == len(TASKS)

    def test_harm_has_enough_eligible_tasks(self):
        # H7 counts harm only on K1 and K6 (K7s cannot show harm against `without`, which cannot
        # know the rule) and needs at least 12 such tasks to report anything.
        assert sum(t["class"] in ("K1", "K6") for t in TASKS) >= 12

    def test_at_least_three_tasks_are_real_sourced(self):
        real = [t["id"] for t in TASKS if t.get("real_source")]
        assert len(real) >= 3 and all(BY_ID[i]["class"] in ("K3", "K4", "K5") for i in real), real

    def test_shape_matches_the_plan(self):
        for t in TASKS:
            if t["class"] == "K7c":  # stops instead of implementing: scored by clarification
                assert t["check_cmd"] == t["functional_cmd"] == "", t["id"]
                assert len(t["conflict_decisions"]) == 2 and "needed_decision" not in t, t["id"]
                assert len(t["clarification"]["sides"]) == 2, t["id"]
                assert t["clarification"]["side_taken_cmd"].startswith(PY), t["id"]
                continue
            assert t["functional_cmd"].startswith(PY), t["id"]
            assert t["seed_decisions"] == TASKS[0]["seed_decisions"]  # one shared store
            assert t["store_order_seed"] == TASKS[0]["store_order_seed"]
            # Decisions that apply beyond what is graded, recorded so their cost can be read.
            assert all(0 <= i < len(t["seed_decisions"]) and i != t.get("needed_decision")
                       for i in t["secondary_decisions"]), t["id"]
            needs = t["class"] in ("K2", "K3", "K4", "K5", "K7s")
            assert ("needed_decision" in t) is needs, t["id"]
            if t["class"] == "K6":
                assert t["check_cmd"] == "", t["id"]  # no decision bears on it
            else:
                assert t["check_cmd"].startswith(PY), t["id"]
            if t["class"] in ("K3", "K4", "K5"):
                assert t["tag"] in ("convention", "judgment") and t["plausible_wrong"], t["id"]
            if t["class"] in ("K4", "K5"):
                assert t["tag"] == "judgment", t["id"]
            if t["class"] == "K7s":  # the needed decision supersedes an older revision
                assert t["seed_decisions"][t["needed_decision"]]["history"], t["id"]

    def test_needed_decisions_come_in_every_author_style(self):
        for t in TASKS:
            if t["class"] in ("K2", "K3", "K4", "K5"):
                variants = t["seed_decisions"][t["needed_decision"]].get("variants") or []
                assert [v["style"] for v in variants] == ["terse", "narrative", "plan"], t["id"]

    @pytest.mark.parametrize("task", TASKS, ids=list(BY_ID))
    def test_prompt_does_not_carry_the_decision(self, task):
        for token in LEAKS.get(task["id"], []):
            assert token not in task["prompt"], (task["id"], token)


@pytest.mark.parametrize(("task_id", "label", "extra", "adherence", "functional"), KIT,
                         ids=[f"{t}-{label}" for t, label, *_ in KIT])
def test_checks_judge_each_patch(task_id, label, extra, adherence, functional, golden, tmp_path):
    task = BY_ID[task_id]
    work = _work(task, golden, tmp_path, extra)
    assert _passes(task["check_cmd"], work) is adherence, "adherence"
    assert _passes(task["functional_cmd"], work) is functional, "functional"


def test_every_task_has_the_four_kit_cases():
    for task_id, task in BY_ID.items():
        if task["class"] == "K7c":
            labels = {label for tid, label, *_ in CLARIFY_KIT if tid == task_id}
            assert len(labels) == 6, task_id
            continue
        cases = {(a, f) for tid, _, _, a, f in KIT if tid == task_id}
        if task["check_cmd"]:
            assert cases >= {(True, True), (False, True), (True, False), (False, False)}, task_id
        else:
            assert cases == {(None, True), (None, False)}, task_id


@pytest.mark.parametrize("task", TASKS, ids=list(BY_ID))
def test_the_untouched_fixture_never_succeeds(task, golden, tmp_path):
    if task["class"] == "K7c":
        pytest.skip("scored by clarification, which needs the session's final message")
    work = _work(task, golden, tmp_path)
    adherence, functional = _passes(task["check_cmd"], work), _passes(task["functional_cmd"], work)
    assert functional is False or adherence is False
    # Adherence alone may pass an untouched fixture only for a decision that forbids an action
    # (K5: "don't retry"); everywhere else it must measure something the agent did.
    if task["check_cmd"] and not task["forbids_action"]:
        assert adherence is False


class TestOverlay:
    def test_write_append_replace_then_commit(self, golden, tmp_path):
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        apply_overlay(work, [{"path": "README.md", "content": "svc {seed}\n"},
                             {"path": "app/svc_{seed}_core.py", "append": "\n# tail {seed}\n"},
                             {"path": "app/svc_{seed}_core.py", "old": "slot\": 3}",
                              "new": "slot\": 3}  # three"}], SEED)
        core = (work / "app" / f"svc_{SEED}_core.py").read_text()
        assert (work / "README.md").read_text() == f"svc {SEED}\n"
        assert core.endswith(f"\n# tail {SEED}\n") and '"slot": 3}  # three' in core
        status = subprocess.run(["git", "status", "--porcelain"], cwd=work,
                                capture_output=True, text=True).stdout
        assert status == ""  # committed, so a session's diff starts after the overlay

    @pytest.mark.parametrize("path", ["../escape.txt", "/tmp/escape.txt"])
    def test_an_overlay_cannot_write_outside_the_fixture(self, golden, tmp_path, path):
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        with pytest.raises(ValueError, match="outside the fixture"):
            apply_overlay(work, [{"path": path, "content": "x"}], SEED)

    def test_a_replace_that_does_not_match_exactly_once_fails(self, golden, tmp_path):
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        with pytest.raises(ValueError, match="exactly once"):
            apply_overlay(work, [{"path": "app/svc_{seed}_core.py", "old": "def ", "new": "x"}],
                          SEED)


def test_docs_indexed_holds_every_decision_once(golden, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(golden, work)
    items = seeding.seed_items(TASKS[0], SEED)
    run._docs_indexed_setup(work, items)
    index = (work / "CLAUDE.md").read_text()
    records = sorted((work / "docs" / "decisions").glob("*.md"))
    # One record per revision: a superseded rule keeps its own record beside its replacement.
    revisions = [rev for item in items for rev in item["history"] + [item]]
    assert len(records) == len(revisions)
    for revision, record in zip(revisions, records):
        assert f"](docs/decisions/{record.name})" in index
        assert run._decision_title(revision) in index
        assert revision["content"] in record.read_text()


class TestSteadyState:
    def _setup_code(self, tmp_path, monkeypatch, steady):
        commands = []
        monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: commands.append(args))
        repo = tmp_path / "repo"
        run._condition_b_setup(str(repo), tmp_path / "home", "", steady=steady,
                               seed_decisions=seeding.seed_items(BY_ID["adopt-k5-no-retry"], SEED))
        return repo, commands[-1][-1]

    def _exec_in_sandbox(self, code, repo, golden, tmp_path, monkeypatch):
        from contexer import store
        shutil.copytree(golden, repo)
        apply_overlay(repo, BY_ID["adopt-k5-no-retry"]["fixture_files"], SEED)
        monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "store")
        exec(compile(code, "<setup>", "exec"), {})
        return store

    def test_steady_setup_leaves_the_bootstrap_prompt_silent(self, golden, tmp_path, monkeypatch):
        repo, code = self._setup_code(tmp_path, monkeypatch, steady=True)
        monkeypatch.undo()
        store = self._exec_in_sandbox(code, repo, golden, tmp_path, monkeypatch)
        assert store.bootstrap_prompt_payload(str(repo), "Add a function") == {
            "status": "", "context": ""}

    def test_the_steady_check_fails_when_bootstrap_is_unfinished(self, golden, tmp_path,
                                                                 monkeypatch):
        # Mutation check: the same setup without finishing bootstrap must trip the gate.
        repo, code = self._setup_code(tmp_path, monkeypatch, steady=False)
        monkeypatch.undo()
        code += seeding.steady_check_script(str(repo))
        with pytest.raises(AssertionError, match="bootstrap prompt still due"):
            self._exec_in_sandbox(code, repo, golden, tmp_path, monkeypatch)


STUB = """#!/bin/sh
echo '{"result": "stub", "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0, "num_turns": 1, "duration_ms": 1, "session_id": "stub"}'
"""


def test_stub_campaign_scores_both_checks(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["adopt-k4-batch-legacy"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without", "docs_indexed"), wait_for_otel=False,
                           tasks_file=TASKS_FILE, steady_state=True)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["condition"] for r in rows] == ["without", "docs_indexed"]
    for row in rows:
        assert row["error"] == ""
        # The stub writes no code: fetch_records_batch is missing, so both checks fail.
        assert row["adherence"] is False and row["functional"] is False
        assert row["success"] is False
        assert "[adherence]" in row["check_output"] and "[functional]" in row["check_output"]
    assert json.loads((out.parent / "campaign.json").read_text())["steady_state"] is True


def test_a_session_that_follows_the_decision_but_breaks_the_code_is_not_a_success(tmp_path):
    # The runner's own scoring, end to end: a fake session writes the K4 batch reader exactly as
    # the decision says, but breaks the legacy reader. Adherence passes, functional fails, so
    # the row must not be a success.
    patch = K4_GOOD + K4_BREAK_LEGACY
    stub = tmp_path / "claude"
    stub.write_text(STUB.replace("#!/bin/sh\n", "#!/bin/sh\ncat >> app/svc_7_core.py <<'PATCH'\n"
                                 + patch + "PATCH\n", 1))
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["adopt-k4-batch-legacy"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without",), wait_for_otel=False, tasks_file=TASKS_FILE)
    row = json.loads(out.read_text().splitlines()[0])
    assert row["error"] == ""
    assert row["adherence"] is True and row["functional"] is False
    assert row["success"] is False
    assert "[functional]" in row["check_output"] and "[adherence]" not in row["check_output"]


def test_preview_counts_only_tasks_that_need_a_decision():
    from benchmarks import replay_delivery
    report = replay_delivery.replay_tasks(TASKS_FILE, SEED, steady=True)
    s = report["summary"]
    labelled = sum("needed_decision" in t for t in TASKS) * 3
    assert s["tasks"] == labelled and s["unlabelled"] == len(TASKS) * 3 - labelled
    assert set(s["by_style"]) <= {"terse", "narrative", "plan", ""}
    # Every seed, superseding revisions and conflicting pairs included, reaches the store. How
    # many needed decisions are then delivered is a measurement the preview reports, not a
    # harness property, so it is not asserted here.
    assert s["seeds_not_stored"] == 0
    assert s["needed_full"] + s["needed_named_only"] + s["needed_missing"] == labelled
    assert report["steady_state"] is True and s["unsteady_runs"] == 0


SUPERSEDED = {"subtype": "architecture", "source_files": ["app/svc_{seed}_core.py"],
              "title": "Time out upstream calls after 2 seconds", "date": "2026-09-15",
              "content": "Upstream calls in app/svc_{seed}_core.py time out after 2 seconds, "
                         "because the #88 latency budget replaced the old 5 second limit.",
              "history": [{"title": "Time out upstream calls after 5 seconds", "date": "2026-06-01",
                           "content": "Upstream calls in app/svc_{seed}_core.py time out after 5 "
                                      "seconds, because the gateway allows 10."}]}


class TestSupersededDecisions:
    def test_seeding_stores_the_old_rule_as_history_and_serves_the_new_one(self, golden, tmp_path,
                                                                          monkeypatch):
        from contexer import revisions, store
        repo = tmp_path / "repo"
        shutil.copytree(golden, repo)
        monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "store")
        items = seeding.seed_items({"seed_decisions": [SUPERSEDED]}, SEED)
        scope = {}
        exec(compile(seeding.bootstrap_script(str(repo)) + seeding.seed_script(str(repo), items),
                     "<seed>", "exec"), scope)
        entry = next(e for e in store.load(str(repo))["entries"]
                     if e["id"].startswith(scope["seeded_ids"][0]))
        assert len(entry["revisions"]) == 2
        assert "2 seconds" in revisions.current_content(entry)
        assert not entry.get("proposed_revision")  # the superseding revision was approved

    def test_static_arms_show_both_revisions_dated_with_the_old_one_marked(self, golden, tmp_path):
        items = seeding.seed_items({"seed_decisions": [SUPERSEDED]}, SEED)
        old, new = run._static_entries(items[0])
        assert "5 seconds" in old and "superseded on 2026-09-15" in old
        assert "In force from 2026-06-01" in old
        assert "2 seconds" in new and "Decided 2026-09-15" in new and "replaces" in new
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        run._docs_indexed_setup(work, items)
        old_rec, new_rec = sorted((work / "docs" / "decisions").glob("*.md"))
        assert "Status: superseded on 2026-09-15 by " + new_rec.name in old_rec.read_text()
        assert "In force from: 2026-06-01" in old_rec.read_text()
        assert "supersedes " + old_rec.name in new_rec.read_text()
        assert "(superseded)" in (work / "CLAUDE.md").read_text()


class TestClarificationScoring:
    SIDES = [["deleted': true", "flag"], ["RecordGone", "raise"]]

    @pytest.mark.parametrize(("text", "side_taken", "expected"), [
        ("Two decisions conflict: one says return a flag, the other says raise RecordGone. "
         "Which should win?", False, True),                       # valid clarification
        ("This is unclear, so I stopped.", False, False),        # vague hesitation
        ("Implemented the deleted flag.", True, False),          # took side A
        ("Raised RecordGone for deleted ids.", True, False),     # took side B
        ("Decisions conflict (flag vs RecordGone); I went with the flag.", True, False),
    ])
    def test_only_naming_both_sides_without_taking_one_passes(self, text, side_taken, expected):
        assert run._clarified(text, self.SIDES, side_taken) is expected


@pytest.mark.parametrize(("task_id", "label", "text", "extra", "expected"), CLARIFY_KIT,
                         ids=[f"{t}-{label}" for t, label, *_ in CLARIFY_KIT])
def test_clarification_check_judges_each_outcome(task_id, label, text, extra, expected, golden,
                                                 tmp_path):
    task = BY_ID[task_id]
    work = _work(task, golden, tmp_path, extra)
    cmd = task["clarification"]["side_taken_cmd"].replace("{seed}", str(SEED))[len(PY):-1]
    taken = subprocess.run([sys.executable, "-c", cmd], cwd=work, capture_output=True,
                           timeout=60).returncode != 1  # the runner's rule: only exit 1 is "no side"
    assert taken is bool(extra), "side_taken_cmd misjudged the code"
    assert run._clarified(text, task["clarification"]["sides"], taken) is expected


@pytest.mark.parametrize(("patch", "clarified"), [("", True),
                                                  ("def slot_label(slot):\n    return 'x'\n", True),
                                                  ("def slot_label(slot):\n"
                                                   "    return f'slot-{slot:02d}'\n", False)])
def test_runner_scores_k7c_by_clarification(tmp_path, patch, clarified):
    # The runner's own scoring, end to end: the stub's final message names both sides. Writing no
    # code, or code that takes neither side, is a clarification; implementing a side is not.
    text = "Two decisions conflict: lowercase slot-07 vs uppercase SLOT_07. Which applies?"
    stub = tmp_path / "claude"
    body = STUB.replace('"result": "stub"', f'"result": "{text}"')
    if patch:
        body = body.replace("#!/bin/sh\n", "#!/bin/sh\ncat >> app/svc_7_core.py <<'PATCH'\n\n"
                            + patch + "PATCH\n", 1)
    stub.write_text(body)
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["adopt-k7c-label"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("claudemd_full",), wait_for_otel=False, tasks_file=TASKS_FILE)
    row = json.loads(out.read_text().splitlines()[0])
    assert row["error"] == ""
    assert row["clarified"] is clarified and row["success"] is clarified


@pytest.mark.parametrize("task", [t for t in TASKS if t["class"] == "K7c"], ids=lambda t: t["id"])
def test_k7c_sides_are_stored_at_the_same_status(task):
    # A K7c pair has no recorded winner. If the store queued one side for approval (seeding then
    # approves it) and left the other suggested, Contexer would correctly show a winner the task
    # says doesn't exist: "instead of raising" once did exactly that to the tombstone rule.
    from contexer import store
    levels = {store._classify_level(task["seed_decisions"][i]["content"],
                                    task["seed_decisions"][i]["subtype"], "ai")
              for i in task["conflict_decisions"]}
    assert len(levels) == 1, levels


def test_the_150_decision_file_keeps_every_task_and_points_at_the_same_decisions():
    # The larger store changes only the decisions around each task: prompts and checks are
    # identical, and every remapped index still names the decision it named before.
    big = json.loads((TASKS_FILE.parent / "adoption_tasks_150.json").read_text())
    assert [t["id"] for t in big] == [t["id"] for t in TASKS]
    for small, large in zip(TASKS, big):
        for key in ("prompt", "check_cmd", "functional_cmd", "class", "fixture_files",
                    "clarification"):
            assert small.get(key) == large.get(key), (small["id"], key)
        assert len(large["seed_decisions"]) == 150
        for key in ("needed_decision", "secondary_decisions", "conflict_decisions"):
            a, b = small.get(key), large.get(key)
            a = [] if a is None else a if isinstance(a, list) else [a]
            b = [] if b is None else b if isinstance(b, list) else [b]
            assert [small["seed_decisions"][i] for i in a] == \
                [large["seed_decisions"][i] for i in b], (small["id"], key)
        task_file = "app/svc_{seed}_core.py"
        extra = [d for d in large["seed_decisions"] if d not in small["seed_decisions"]]
        assert not any(task_file in d.get("source_files", []) or "app/" in d.get("source_files", [])
                       for d in extra), small["id"]


def test_a_broken_overlay_stops_the_campaign_before_any_session(tmp_path):
    tasks = json.loads(TASKS_FILE.read_text())
    broken = next(t for t in tasks if t["fixture_files"])
    broken["fixture_files"] = [{"path": "app/svc_{seed}_core.py", "old": "no such text", "new": "x"}]
    bad = tmp_path / "tasks.json"
    bad.write_text(json.dumps(tasks))
    with pytest.raises(ValueError, match="exactly once"):
        run.run_campaign(tmp_path / "camp", reps=1, claude_cmd="/nonexistent/claude", seed=SEED,
                         conditions=("without",), wait_for_otel=False, tasks_file=bad)
    assert not (tmp_path / "camp" / "runs.jsonl").exists()


def test_an_output_dir_never_mixes_steady_and_first_install_runs(tmp_path):
    import hashlib
    out = tmp_path / "camp"
    out.mkdir()
    (out / "runs.jsonl").write_text('{"task_id": "adopt-k6-slots"}\n')
    (out / "campaign.json").write_text(json.dumps({
        "tasks_sha256": hashlib.sha256(TASKS_FILE.read_bytes()).hexdigest(), "steady_state": False}))
    with pytest.raises(ValueError, match="steady_state"):
        run.run_campaign(out, reps=1, conditions=("without",), wait_for_otel=False,
                         tasks_file=TASKS_FILE, steady_state=True)


def test_docs_indexed_keeps_a_legacy_single_decision(golden, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(golden, work)
    run._docs_indexed_setup(work, [{"content": "Never log request ids.", "subtype": "constraint"}])
    assert "Never log request ids." in next((work / "docs" / "decisions").glob("*.md")).read_text()


def test_a_superseding_revision_that_is_refused_fails_setup(golden, tmp_path, monkeypatch):
    # If the store refuses the newer revision, the old rule would be served as current: setup
    # must fail rather than seed a stale store silently.
    from contexer import server, store
    repo = tmp_path / "repo"
    shutil.copytree(golden, repo)
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "store")
    real = server.update_context

    def refuse_replacements(content, **kw):
        if kw.get("replace_id"):
            return "Correction NOT stored."
        return real(content, **kw)

    monkeypatch.setattr(server, "update_context", refuse_replacements)
    items = seeding.seed_items({"seed_decisions": [SUPERSEDED]}, SEED)
    with pytest.raises(AssertionError, match="revision not current"):
        exec(compile(seeding.bootstrap_script(str(repo)) + seeding.seed_script(str(repo), items),
                     "<seed>", "exec"), {})
