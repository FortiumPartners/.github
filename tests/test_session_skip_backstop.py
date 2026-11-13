"""Tests for FortiumPartners/.github#4 in .github/workflows/claude.yml.

PRs labelled `claude-session` skip the paid security review at open. A merged
one without `loop-clean` gets a backstop review that files an issue. A push
clears a stale `loop-clean`.

Each case runs a step's own `run:` script the way Actions runs it
(`bash --noprofile --norc -eo pipefail`), with a stub `gh` on PATH that logs
its arguments and answers from the STUB_* environment. The `if:` expressions
are proved by the e2e in the PR, not here.

Run: scripts/run-tests.sh
"""

import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/claude.yml"

# A stub gh: logs one line of args per call, answers by endpoint.
GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *"/events?"*)
    [ -n "${STUB_EVENTS_FAIL:-}" ] && { echo "gh: Server Error (HTTP 502)" >&2; exit 1; }
    for t in ${STUB_LABELED_AT//,/ }; do echo "labeled $t"; done
    for t in ${STUB_FORCED_AT//,/ }; do echo "head_ref_force_pushed $t"; done
    exit 0 ;;
  *"/actions/runs?"*) printf '%s' "${STUB_SUITES_AT:-}" | tr ',' '\n'; exit 0 ;;
  *"/labels"*"--jq"*) printf '%s' "${STUB_LABELS:-}" | tr ',' '\n'; exit 0 ;;
  "issue list"*) printf '%s' "${STUB_EXISTING:-[]}"; exit 0 ;;
  "api -X DELETE"*)
    case "${STUB_DELETE:-ok}" in
      ok) exit 0 ;;
      404) echo "gh: Label does not exist (HTTP 404)" >&2; exit 1 ;;
      *) echo "gh: Server Error (HTTP 500)" >&2; exit 1 ;;
    esac ;;
  "api repos/"*"/labels/"*) [ "${STUB_LABEL_EXISTS:-yes}" = yes ] && exit 0; exit 1 ;;
  "label create"*) exit 0 ;;
  "issue create"*)
    while [ $# -gt 0 ]; do [ "$1" = "--body-file" ] && cp "$2" "$GH_BODY"; shift; done
    echo "https://github.com/o/r/issues/99"; exit 0 ;;
esac
echo "stub gh: unexpected call: $*" >&2; exit 2
"""


def jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def step(job, step_id):
    matches = [s for s in jobs()[job]["steps"] if s.get("id") == step_id]
    if len(matches) != 1:
        raise AssertionError(f"expected one step {step_id!r} in job {job!r}, found {len(matches)}")
    return matches[0]


def run_step(job, step_id, env=None, files=None):
    """Run one step's script. Returns (proc, gh_calls, outputs, body)."""
    script = step(job, step_id)["run"]
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        bindir = ws / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        for name, content in (files or {}).items():
            (ws / name).write_text(content)
        (ws / "step.sh").write_text(script)
        full_env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GH_LOG": str(ws / "gh.log"),
            "GH_BODY": str(ws / "body.out"),
            "GITHUB_OUTPUT": str(ws / "output"),
            "GITHUB_STEP_SUMMARY": str(ws / "summary"),
            "REPO": "o/r",
            "PR": "42",
            "RUN_URL": "https://github.com/o/r/actions/runs/1",
        }
        full_env.update(env or {})
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "step.sh"],
            cwd=ws, env=full_env, capture_output=True, text=True,
        )
        read = lambda p: p.read_text() if p.exists() else ""
        calls = [c for c in read(ws / "gh.log").splitlines() if c]
        outputs = dict(l.split("=", 1) for l in read(ws / "output").splitlines() if "=" in l)
        return proc, calls, outputs, read(ws / "body.out")


def fails(proc):
    return f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"


class PrLabels(unittest.TestCase):
    def outputs(self, labels):
        proc, _, out, _ = run_step("pr-labels", "labels", {"STUB_LABELS": labels})
        self.assertEqual(proc.returncode, 0, fails(proc))
        return out

    def test_session_pr(self):
        self.assertEqual(self.outputs("claude-session"), {"session": "true"})

    def test_unlabelled_pr(self):
        self.assertEqual(self.outputs(""), {"session": "false"})

    def test_both_labels(self):
        self.assertEqual(self.outputs("bug,loop-clean,claude-session"), {"session": "true"})

    def test_label_name_must_match_whole(self):
        self.assertEqual(self.outputs("not-claude-session,loop-clean-ish"), {"session": "false"})

    def test_reads_past_the_default_page(self):
        # The labels endpoint returns 30 per page by default.
        _, calls, _, _ = run_step("pr-labels", "labels", {"STUB_LABELS": ""})
        self.assertIn("per_page=100", calls[0])


class PrLabelsAtMerge(unittest.TestCase):
    """On a merge, loop-clean exempts the PR only if applied after the head was pushed."""

    PUSH = "2026-11-12T14:48:55Z"

    def run_closed(self, labels=("claude-session", "loop-clean"), **env):
        base = {"ACTION": "closed", "PAYLOAD_LABELS": json.dumps(list(labels)),
                "HEAD_SHA": "abc123", "HEAD_REF": "feat/x", "STUB_SUITES_AT": self.PUSH}
        return run_step("pr-labels", "labels", {**base, **env})

    def clean(self, **env):
        proc, calls, out, _ = self.run_closed(**env)
        self.assertEqual(proc.returncode, 0, fails(proc))
        return out.get("clean_at_head"), calls

    def test_label_after_push_exempts(self):
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:51:44Z")[0], "true")

    def test_stale_label_from_before_push_does_not(self):
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:48:46Z")[0], "false")

    def test_last_labeling_counts(self):
        both = "2026-11-12T14:51:44Z,2026-11-12T14:48:46Z"
        self.assertEqual(self.clean(STUB_LABELED_AT=both)[0], "true")

    def test_same_second_does_not(self):
        self.assertEqual(self.clean(STUB_LABELED_AT=self.PUSH)[0], "false")

    def test_earliest_suite_is_the_push(self):
        suites = "2026-11-12T14:51:53Z,2026-11-12T14:48:55Z"
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:50:00Z",
                                    STUB_SUITES_AT=suites)[0], "true")

    def test_force_push_after_label_does_not(self):
        # A force push back to an old SHA keeps that SHA's early first run.
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:51:44Z",
                                    STUB_FORCED_AT="2026-11-12T14:55:00Z")[0], "false")

    def test_force_push_before_label_exempts(self):
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:51:44Z",
                                    STUB_FORCED_AT="2026-11-12T14:50:00Z")[0], "true")

    def test_no_suite_does_not(self):
        self.assertEqual(self.clean(STUB_LABELED_AT="2026-11-12T14:51:44Z",
                                    STUB_SUITES_AT="")[0], "false")

    def test_no_labeled_event_does_not(self):
        self.assertEqual(self.clean(STUB_LABELED_AT="")[0], "false")

    def test_no_loop_clean_skips_the_lookup(self):
        value, calls = self.clean(labels=("claude-session",))
        self.assertEqual(value, "false")
        self.assertEqual(calls, [])

    def test_runs_are_read_for_the_head_on_its_branch(self):
        _, calls = self.clean(STUB_LABELED_AT="2026-11-12T14:51:44Z")
        runs = [c for c in calls if "actions/runs" in c]
        self.assertEqual(len(runs), 1, calls)
        self.assertIn("head_sha=abc123&event=pull_request", runs[0])
        self.assertIn("env.HEAD_REF", runs[0])

    def test_needs_no_permission_callers_lack(self):
        # A nested job asking for more than the caller grants fails the run.
        self.assertEqual(set(jobs()["pr-labels"]["permissions"]),
                         {"actions", "issues", "pull-requests"})

    def test_lookup_failure_fails_the_step(self):
        # An empty output then reads as not clean, and the backstop runs.
        proc, _, out, _ = self.run_closed(STUB_EVENTS_FAIL="1")
        self.assertNotEqual(proc.returncode, 0, fails(proc))
        self.assertNotEqual(out.get("clean_at_head"), "true")


class ClearLoopClean(unittest.TestCase):
    def test_removes_label(self):
        proc, calls, _, _ = run_step("clear-loop-clean", "clear", {"STUB_DELETE": "ok"})
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertEqual(calls, ["api -X DELETE repos/o/r/issues/42/labels/loop-clean"])

    def test_already_gone_is_success(self):
        proc, _, _, _ = run_step("clear-loop-clean", "clear", {"STUB_DELETE": "404"})
        self.assertEqual(proc.returncode, 0, fails(proc))

    def test_other_error_fails(self):
        proc, _, _, _ = run_step("clear-loop-clean", "clear", {"STUB_DELETE": "500"})
        self.assertNotEqual(proc.returncode, 0, fails(proc))
        self.assertIn("::error::", proc.stdout)


FINDINGS = {
    "findings": [
        {"file": "api/auth.py", "line": 12, "severity": "HIGH", "category": "auth_bypass",
         "description": "Token check skipped", "recommendation": "Check the token"},
        {"file": "web/x.js", "line": 3, "severity": "MEDIUM", "category": "xss",
         "description": "`id` ::warning::injected", "recommendation": "Escape"},
    ]
}


class BackstopIssue(unittest.TestCase):
    def run_file(self, results, review="success", **env):
        files = {"claudecode-results.json": json.dumps(results)} if results is not None else {}
        return run_step("backstop-issue", "file-issue", {"REVIEW_RESULT": review, **env}, files)

    def issue_calls(self, calls):
        return [c for c in calls if c.startswith("issue create")]

    def test_no_findings_files_nothing(self):
        proc, calls, _, _ = self.run_file({"findings": []})
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertEqual(self.issue_calls(calls), [])

    def test_findings_file_one_issue(self):
        proc, calls, _, body = self.run_file(FINDINGS)
        self.assertEqual(proc.returncode, 0, fails(proc))
        issues = self.issue_calls(calls)
        self.assertEqual(len(issues), 1, calls)
        self.assertIn("2 finding(s) in merged PR #42", issues[0])
        for label in ("--label bug", "--label security", "--label P1"):
            self.assertIn(label, issues[0])
        self.assertIn("api/auth.py:12", body)
        self.assertIn("web/x.js:3", body)
        self.assertIn("https://github.com/o/r/pull/42", body)

    def test_no_high_finding_is_p2(self):
        medium = {"findings": [FINDINGS["findings"][1]]}
        proc, calls, _, _ = self.run_file(medium)
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertIn("--label P2", self.issue_calls(calls)[0])

    def test_finding_text_stays_literal(self):
        with tempfile.TemporaryDirectory() as d:
            marker = pathlib.Path(d) / "pwned"
            text = f"$(touch {marker}) `id` ::warning::injected"
            finding = dict(FINDINGS["findings"][1], description=text)
            proc, _, _, body = self.run_file({"findings": [finding]})
            self.assertEqual(proc.returncode, 0, fails(proc))
            self.assertIn(text, body)
            self.assertNotIn("::warning::", proc.stdout)
            self.assertFalse(marker.exists())

    def test_mentions_do_not_notify(self):
        finding = dict(FINDINGS["findings"][1], description="ping @octocat and @org/team",
                       recommendation="ask @octocat")
        proc, _, _, body = self.run_file({"findings": [finding]})
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertNotRegex(body, r"@[A-Za-z0-9]")
        self.assertIn("octocat", body)

    def test_non_string_severity(self):
        finding = dict(FINDINGS["findings"][1], severity=3)
        proc, calls, _, _ = self.run_file({"findings": [finding]})
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertIn("--label P2", self.issue_calls(calls)[0])

    def existing(self, title):
        return json.dumps([{"url": "https://github.com/o/r/issues/7", "title": title}])

    def test_rerun_does_not_duplicate_open_issue(self):
        proc, calls, _, _ = self.run_file(
            FINDINGS, STUB_EXISTING=self.existing("security-review backstop: 2 finding(s) in merged PR #42"))
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertEqual(self.issue_calls(calls), [])
        self.assertIn("issues/7", proc.stdout)

    def test_near_title_is_not_a_duplicate(self):
        # Title search is fuzzy: PR #42's search also returns #420's issue.
        proc, calls, _, _ = self.run_file(
            FINDINGS, STUB_EXISTING=self.existing("security-review backstop: 2 finding(s) in merged PR #420"))
        self.assertEqual(proc.returncode, 0, fails(proc))
        self.assertEqual(len(self.issue_calls(calls)), 1, calls)

    def test_only_the_workflows_own_issues_count(self):
        # Anyone who can open an issue could otherwise pre-empt the backstop.
        _, calls, _, _ = self.run_file(FINDINGS, STUB_EXISTING="[]")
        searches = [c for c in calls if c.startswith("issue list")]
        self.assertEqual(len(searches), 1, calls)
        self.assertIn("--author app/github-actions", searches[0])

    def test_failed_review_files_not_reviewed_issue(self):
        proc, calls, _, body = self.run_file(None, review="failure")
        self.assertEqual(proc.returncode, 0, fails(proc))
        issues = self.issue_calls(calls)
        self.assertEqual(len(issues), 1, calls)
        self.assertIn("merged PR #42 was not reviewed", issues[0])
        self.assertIn("--label P1", issues[0])
        self.assertIn("https://github.com/o/r/actions/runs/1", body)

    def test_missing_label_is_created_first(self):
        proc, calls, _, _ = self.run_file(FINDINGS, STUB_LABEL_EXISTS="no")
        self.assertEqual(proc.returncode, 0, fails(proc))
        creates = [i for i, c in enumerate(calls) if c.startswith("label create")]
        issue = next(i for i, c in enumerate(calls) if c.startswith("issue create"))
        self.assertTrue(creates and max(creates) < issue, calls)


class Wiring(unittest.TestCase):
    def test_security_review_needs_pr_labels(self):
        self.assertEqual(jobs()["security-review"].get("needs"), "pr-labels")
        self.assertEqual(set(jobs()["pr-labels"]["outputs"]), {"session", "clean_at_head"})

    def test_backstop_trusts_only_a_label_newer_than_the_push(self):
        cond = jobs()["security-review"]["if"]
        self.assertIn("needs.pr-labels.outputs.clean_at_head != 'true'", cond)
        self.assertNotIn("'loop-clean'", cond)
        self.assertIn("closed", jobs()["pr-labels"]["if"])

    def test_backstop_turns_off_pr_comment(self):
        action = next(s for s in jobs()["security-review"]["steps"]
                      if s.get("uses", "").startswith("anthropics/claude-code-security-review@"))
        self.assertIn("closed", str(action["with"].get("comment-pr")))

    def test_only_backstop_issue_can_write_issues(self):
        for name, job in jobs().items():
            perms = job.get("permissions", {})
            if perms.get("issues") == "write" and name not in ("claude", "backstop-issue"):
                self.fail(f"job {name} holds issues: write")
        self.assertEqual(jobs()["backstop-issue"]["permissions"].get("issues"), "write")
        self.assertNotIn("issues", jobs()["security-review"]["permissions"])

    def test_new_jobs_pin_actions_by_sha(self):
        # The older jobs' actions/checkout@v4 is FortiumPartners/.github#9.
        for name in ("pr-labels", "clear-loop-clean", "backstop-issue"):
            for s in jobs()[name].get("steps", []):
                uses = s.get("uses")
                if uses and not uses.startswith("./"):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$", f"{name}: {uses}")
            self.assertTrue(jobs()[name].get("steps"), name)

    def test_caller_template_lists_closed(self):
        template = re.search(r"#\s+pull_request:\n#\s+types: \[([^\]]*)\]", WORKFLOW.read_text())
        self.assertIsNotNone(template)
        self.assertIn("closed", template.group(1))


if __name__ == "__main__":
    unittest.main()
