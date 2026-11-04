# Debian13_orginal — safe manual updates

The on-screen **Synchronizuj dane** button only refreshes API data. It does
**not** download, checkout, deploy or restart any source code. The separate
`debian13/update_preflight.py` script is a read-only **release evidence gate**,
not an updater, and does not require an autonomous AI agent or paid API tokens.

## Before manually updating any running installation

1. Review changes and tests in the relevant GitHub pull request. Do not
   deploy a draft PR or directly run code copied from an unreviewed branch.
2. Only after an authorized merge, use a **clean checkout of the `main`
   branch** and verify the current GitHub `main` commit.
3. Run the preflight from the repository root:

   ```sh
   python3 debian13/update_preflight.py
   # Or for machine-readable status:
   python3 debian13/update_preflight.py --json
   ```

   Exit status `0` means **eligible for manual release review only**. Exit
   status `1` means **blocked** or unverifiable. The tool refuses to approve
   a dirty checkout, non-main branch, stale HEAD, missing successful validation,
   failed/cancelled/pending validation, or a successful PR-only CI run.

4. GitHub Actions **Debian 13 validation** runs on `main` pushes affecting
   this project. The script accepts only completed successful `push` or
   `workflow_dispatch` validation on the **exact** current `main` SHA.
   If GitHub API is unavailable, the script returns a blocked result; it
   never assumes success. Unauthenticated GitHub API calls are rate limited.
5. Before a **separately approved deployment**, document the tested SHA,
   deployment method, resource limits, backup and rollback instructions.
   Back up the application SQLite workspace with a consistent SQLite backup,
   retain the prior deployable version, and confirm which running process,
   Docker container or systemd service will be replaced.
6. After deployment by an authorized operator, verify live `/healthz`,
   authenticated diagnostics, UI loading, persistence, and any configured
   provider with a separately permitted live test. Do not treat successful
   GitHub CI as verification of an external server, real AI execution,
   Debian installer/ISO boot, or production security.

## Security boundary

The preflight fetches only fixed GitHub API read-only endpoints under
`https://api.github.com/repos/mmitek515-netizen/Debian_13`, disables HTTP
redirects, limits response bytes, invokes `git` with no shell, prints no
secret-bearing responses, and never executes new code or mutates the local
Git tree. This repository may contain draft changes not yet available in
`main` and therefore not deployable by this gate.

**No PR merge, GitHub fetch/pull, remote install, token disclosure, paid
model call, root operation, or purchase is performed by preflight.**
