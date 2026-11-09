"""Tests for the security-review gate in .github/workflows/claude.yml.

The upstream claude-code-security-review action turns a refused or crashed
review into a warning plus findings_count=0, so the job concluded `success`
on reviews that never ran (FortiumPartners/.github#6). The gate step reads the
results file the action leaves in the workspace and fails the job unless it
holds a real findings list.

Each case runs the gate step's own `run:` script, taken from the workflow
file, the way Actions runs it: `bash --noprofile --norc -eo pipefail`.

Run: uv run --with pyyaml python -m unittest discover -s tests
"""

import json
import pathlib
import subprocess
import tempfile
import unittest

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/claude.yml"
GATE_ID = "review-gate"
ACTION = "anthropics/claude-code-security-review@"

# The message every review returned from 2026-10-16 to 2026-10-30, wrapped the
# way github_action_audit.py wraps a failed audit.
SPEND_CAP = {
    "error": "Security audit failed: API Error: 400 You have reached your "
    "specified workspace API usage limits. You will regain access on "
    "2026-11-01 at 00:00 UTC."
}


def security_review_steps():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["security-review"]["steps"]


def gate_step():
    matches = [s for s in security_review_steps() if s.get("id") == GATE_ID]
    if len(matches) != 1:
        raise AssertionError(f"expected one step with id {GATE_ID!r}, found {len(matches)}")
    return matches[0]


def run_gate(results):
    """Run the gate in a scratch workspace. `results` is None (no file),
    bytes (written raw) or an object (written as JSON)."""
    script = gate_step()["run"]
    with tempfile.TemporaryDirectory() as workspace:
        ws = pathlib.Path(workspace)
        if results is not None:
            raw = results if isinstance(results, bytes) else json.dumps(results).encode()
            (ws / "claudecode-results.json").write_bytes(raw)
        (ws / "gate.sh").write_text(script)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "gate.sh"],
            cwd=ws,
            capture_output=True,
            text=True,
        )


class GateFailsWhenNoReviewRan(unittest.TestCase):
    def assert_fails(self, results):
        proc = run_gate(results)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        return proc

    def test_spend_cap_refusal(self):
        proc = self.assert_fails(SPEND_CAP)
        self.assertIn("workspace API usage limits", proc.stdout)

    def test_error_key_with_findings_alongside(self):
        self.assert_fails({"error": "boom", "findings": []})

    def test_missing_results_file(self):
        self.assert_fails(None)

    def test_empty_results_file(self):
        self.assert_fails(b"")

    def test_invalid_json(self):
        self.assert_fails(b"Traceback (most recent call last):\n")

    def test_no_findings_list(self):
        self.assert_fails({"pr_number": 7})

    def test_error_text_cannot_inject_workflow_commands(self):
        proc = self.assert_fails({"error": "x\n::add-mask::y\n::stop-commands::z"})
        stray = [l for l in proc.stdout.splitlines() if l.startswith("::") and not l.startswith("::error::")]
        self.assertEqual(stray, [])


class GatePassesWhenReviewRan(unittest.TestCase):
    def test_no_findings(self):
        proc = run_gate({"findings": [], "analysis_summary": {"files_reviewed": 3}})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_high_severity_findings(self):
        # The audit script exits 1 on HIGH findings. That is a review that
        # worked; the action posts the findings as a PR comment.
        proc = run_gate({"findings": [{"severity": "HIGH", "file": "a.py", "line": 1}]})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class GateWiring(unittest.TestCase):
    def test_gate_follows_the_review_action(self):
        steps = security_review_steps()
        action = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith(ACTION))
        gate = next(i for i, s in enumerate(steps) if s.get("id") == GATE_ID)
        self.assertGreater(gate, action)

    def test_every_run_reviews(self):
        # The action caches a "reserved" marker before it calls Claude and
        # skips the scan when a marker exists, so a re-run after a refusal
        # would produce no results. run-every-commit bypasses the marker.
        action = next(s for s in security_review_steps() if s.get("uses", "").startswith(ACTION))
        self.assertEqual(str(action["with"].get("run-every-commit")).lower(), "true")


if __name__ == "__main__":
    unittest.main()
