# #4: skip security-review on session PRs, backstop merged ones

## Context
`security-review` in the shared `.github/workflows/claude.yml` bills key `ci-security-review` about $0.33 per opened PR. 59% of that comes from gateway's session-opened PRs, which `/loop-pr-review` already reviews locally. Burke decided on 2026-10-14 that PRs labelled `claude-session` skip it at open. A backstop reviews any such PR merged without `loop-clean`, and a push clears a stale `loop-clean` (issue comment, 2026-11-01). Session-side labelling is live. #6 (the job fails when the API refuses) merged as `5c69aa8`, and its post-merge e2e passed: in run 42822189264 on `main`, `security-review` concluded failure with the invalid key. First, outside this plan: close #11, then message the six caller sessions with `5c69aa8` (pin bump only). The invalid-key secret stays for the e2e below.

Finding that shapes the design: on PR #7, `gh pr create --label` applied the label 1s after creation, as a separate `labeled` event. So the `opened` payload doesn't carry `claude-session`, and the skip can't trust payload labels.

## Changes (one file plus tests: `.github/workflows/claude.yml`)

1. **New `pr-labels` job** (no Claude call, `pull-requests: read`). It runs on `opened`/`reopened`/`ready_for_review`/`closed` when `enable-security-review` is on. It reads the PR's **live** labels (`gh api repos/$REPO/issues/$N/labels`) and outputs `session` and `loop_clean` (true/false). A label applied after this job reads won't be seen. That direction fails open to *cost* (the review runs), never to an unreviewed merge.

2. **`security-review` job** gets `needs: pr-labels`, and its `if:` becomes one of two modes:
   - **open:** action in opened/reopened/ready_for_review, non-draft, non-bot sender, and `session != 'true'`. Unlabelled PRs are reviewed exactly as today. A session PR shows the job as **skipped**.
   - **backstop:** action `closed`, `pull_request.merged == true`, `session == 'true'`, `loop_clean != 'true'`.
   - Same checkout, action and `review-gate` step as now, so a refused backstop review goes red too. `comment-pr` is off in backstop mode, because the issue is the output. Permissions are unchanged, and the job holds no `issues: write`.
   - Checked: the action fetches the diff from `pulls/{n}` and `pulls/{n}/files` (github_action_audit.py:72-131), so it works on a merged PR. Checkout uses `github.sha`, which is the merge commit.

3. **New `backstop-issue` job**. It runs only in backstop mode, after `security-review` (`always()`, when that result is success or failure). It has `issues: write` and nothing else. It downloads the `security-review-results` artifact with a SHA-pinned `actions/download-artifact`, then:
   - **review ran, 0 findings:** no issue; a job summary line.
   - **review ran, N findings:** opens one issue, titled `security-review backstop: N finding(s) in merged PR #X`. The body is built by jq into a file (file:line, severity, category, description, recommendation) plus links to the PR and run. It's labelled `bug`, `security`, and `P1` if any finding is HIGH, else `P2`; missing labels are created first.
   - **review failed** (refused or crashed): opens an issue saying the merged PR was **not** reviewed, with the run link (`bug`, `P1`). That way a refusal on a merged PR can't go unnoticed, since nobody watches checks on merged PRs.
   - Finding text only ever reaches `--body-file`. It never reaches a shell argument or a `::` workflow command.

4. **New `clear-loop-clean` job.** It runs on `synchronize` when the **payload** labels contain `loop-clean`. The payload is the push-time snapshot, so the loop's own later re-label is never undone. It runs `gh api -X DELETE …/labels/loop-clean` with `pull-requests: write`, and treats HTTP 404 as success, because the session may already have removed it.

5. **Header comment and caller template:** add `closed` to `pull_request.types`, and document both modes and the three new jobs.

## Tests (red first, `tests/`)
Extend the existing harness, which runs each step's own `run:` script under `bash -eo pipefail`, with a stub `gh` on PATH:
- `pr-labels`: label sets → outputs (`session`/`loop_clean` correct; no labels; both).
- `clear-loop-clean`: success → 0; stub 404 → 0; stub 500 → non-zero.
- `backstop-issue`: 0 findings → no `gh issue create`; 2 findings with one HIGH → one create call with P1 and a body holding both file:line entries; failed review → a "not reviewed" issue; finding text containing `$(…)`, backticks and `::warning::` stays literal in the body file and never reaches stdout as a command.
- Wiring: `security-review.needs == pr-labels`, `comment-pr` is off in backstop mode, `backstop-issue` holds only `issues: write`, and every new `uses:` is SHA-pinned (this repo's one recorded defect class is CONFIG: action pins).
The `if:` expressions themselves are proved by the e2e below, not by unit tests.

## Verify (e2e, $0, run in this repo)
Use a throwaway caller (`uses: ./.github/workflows/claude.yml`, types including `closed` and `synchronize`) with the invalid-key secret, as for #6, against a scratch base branch `e2e/base-4`. Merges go into that scratch branch, never `main`. Cases:
1. PR with `claude-session` → `security-review` **skipped**.
2. PR with no labels → `security-review` runs (it fails red on the invalid key, which proves it ran).
3. `claude-session` PR merged into the scratch base without `loop-clean` → backstop runs, fails on the key, and `backstop-issue` files a "not reviewed" issue.
4. Push to a PR carrying `loop-clean` → the label is gone.
5. Merged `claude-session` PR *with* `loop-clean` → no backstop.
Clean up afterwards: close the PRs, delete the scratch branches and secret, close the e2e issues.

Then: `/code-review high <branch>`, open the PR with a `Plan:` line and the red runs, then `/loop-pr-review`. Merge needs Burke's OK (admin bypass, as for #10).

## After merge
- Message each caller session (gateway, partner-connect, payouts, pipelinemgr, ideas, identity) with the SHA. They need to bump the pin **and add `closed` to `on.pull_request.types`**, or the backstop never fires.
- Send the merge SHA to the anthropic-key-monitor session.
