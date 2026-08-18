"""tasks.json shape validation - the runner depends on these exact keys."""
import json
import subprocess
import sys
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent / "benchmarks" / "tasks.json"
KINDS = {"convention", "rationale", "continuity", "efficiency"}


class TestTasksFile:
    def test_shape(self):
        tasks = json.loads(TASKS.read_text())
        assert len(tasks) >= 11
        ids = [t["id"] for t in tasks]
        assert len(ids) == len(set(ids))
        for t in tasks:
            assert t["kind"] in KINDS
            assert t["prompt"].strip()
            assert isinstance(t["gold"], list) and isinstance(t["seed_decision"], str)
            assert isinstance(t["chain"], str) and isinstance(t["step"], int)
            if t["kind"] == "rationale":
                assert t["gold"] and t["seed_decision"]

    def test_chains_are_contiguous(self):
        tasks = json.loads(TASKS.read_text())
        chains = {}
        for t in tasks:
            if t["chain"]:
                chains.setdefault(t["chain"], []).append(t["step"])
        for chain, steps in chains.items():
            assert sorted(steps) == list(range(1, len(steps) + 1)), chain


class TestChainAuditCheck:
    """chain-3-audit's check must be behavioral (calls create_order, asserts a
    lowercase string id) - the old 'int(' grep passed in BOTH conditions and
    discriminated nothing (red-team #6)."""

    @staticmethod
    def _script():
        tasks = json.loads(TASKS.read_text())
        chk = next(t for t in tasks if t["id"] == "chain-3-audit")["check_cmd"]
        chk = chk.replace("{seed}", "5")
        assert chk.startswith('uv run python -c "') and chk.endswith('"')
        return chk[len('uv run python -c "'):-1]

    def _run(self, tmp_path, body):
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        (app / "__init__.py").write_text("")
        (app / "svc_5_orders.py").write_text(body)
        return subprocess.run([sys.executable, "-c", self._script()],
                              cwd=tmp_path, capture_output=True, text=True)

    def test_lowercase_string_id_passes(self, tmp_path):
        proc = self._run(tmp_path,
                         "def create_order(data=None):\n    return {'id': '01hzx4abc'}\n")
        assert proc.returncode == 0, proc.stderr

    def test_int_id_fails(self, tmp_path):
        proc = self._run(tmp_path, "def create_order(data=None):\n    return 42\n")
        assert proc.returncode != 0

    def test_uppercase_id_fails(self, tmp_path):
        proc = self._run(tmp_path,
                         "def create_order(data=None):\n    return '01HZX4ABC'\n")
        assert proc.returncode != 0

    def test_no_arg_signature_tolerated(self, tmp_path):
        proc = self._run(tmp_path, "def create_order():\n    return 'abc123'\n")
        assert proc.returncode == 0, proc.stderr

    def test_unmeetable_interface_fails(self, tmp_path):
        proc = self._run(tmp_path,
                         "def create_order(a, b, c, d):\n    return 'abc'\n")
        assert proc.returncode != 0


class TestPromptVariants:
    def test_variants_mirror_base_except_prompt(self):
        tasks = {t["id"]: t for t in json.loads(TASKS.read_text())}
        variants = [t for t in tasks.values() if t.get("variant_of")]
        assert len(variants) >= 6
        for v in variants:
            base = tasks[v["variant_of"]]
            assert v["prompt"] != base["prompt"]
            for key in ("kind", "gold", "seed_decision", "check_cmd", "chain", "step"):
                assert v[key] == base[key], (v["id"], key)

    def test_variants_excluded_from_default_campaign(self):
        from benchmarks.run import _load_tasks
        default_ids = {t["id"] for t in _load_tasks(None)}
        assert not any("-p1" in i or "-p2" in i for i in default_ids)
        explicit = _load_tasks(["rat-storage-p1"])
        assert [t["id"] for t in explicit] == ["rat-storage-p1"]
