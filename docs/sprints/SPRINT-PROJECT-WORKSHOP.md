# Sprint: Project DIY Workshop

**Status:** In progress — workspace and inspection foundation started  
**Purpose:** Let Dhruv-AI safely take a software project from a repository URL to a built, tested, reviewable result.

## Current implementation and handoff

The `codex/project-workspace-foundation` branch contains an initial foundation:

- A durable task workspace record with source metadata, task branch, sandbox runtime, lifecycle, and cleanup state.
- A private per-task directory creator that does not clone, download, or execute project code.
- Authenticated workspace create/list/get endpoints.
- A bounded, read-only repository inventory endpoint for workspaces that reach `READY`; the safe repository intake step is not implemented yet.
- Bounded `repo.read` line-range and exact-text `repo.search` endpoints for ready workspaces; both skip symlinks and impose file, byte, depth, and result limits.
- A read-only `project_workshop` agent with mission-bound `repo.inspect`, `repo.read`, and `repo.search` tools.
- Path validation rejects traversal and symlinks and confines reads to the configured workspace root.

Continue this branch by implementing safe repository intake and the scoped approval flow before enabling dependency downloads or execution. Then add bounded `repo.diff`, validated `workspace.write_patch`, and allowlisted `workspace.run_check` tools, the isolated sandbox runner, build/repair loop, and Missions UI. Preserve the current working tree and do not merge or deploy.

## ChatGPT to Cursor handoff

Use GitHub as the shared work queue and review record:

1. ChatGPT turns the next sprint item into a GitHub issue with scope, acceptance criteria, constraints, and links to this plan.
2. Cursor opens the issue in Agent mode or starts a Cloud Agent from the repository. A `.cursor/rules` file should tell it to read this sprint plan, work on a task branch, preserve unrelated changes, and open a PR without merging or deploying.
3. Cursor's PR includes the change summary, checks run with results, manual acceptance notes, and any remaining risks. GitHub Actions runs the repository's standard checks.
4. ChatGPT reviews the PR through the connected GitHub repository. Cursor addresses review findings on the same branch; the PR is merged only after the user approves it.
5. Once that loop is stable, Cursor Automations can trigger an agent from selected GitHub events. Keep merge and deploy outside the automation.

ChatGPT and Cursor do not share a single live agent session in this workflow; GitHub issues, branches, and PRs carry the work between them.

Cursor kickoff prompt:

> Use `@project-workshop`. Read `docs/sprints/SPRINT-PROJECT-WORKSHOP.md` and continue the next unfinished work package on the provided branch. First inspect the existing mission, tool, approval, and workspace code. Preserve unrelated changes. Implement only the next cohesive slice, add focused tests, run the required checks, and open a PR with results. Do not merge or deploy. Do not run project code outside the approved sandbox flow.

## Outcome

When Dhruv asks for a project change, Dhruv-AI can inspect the repository, explain a plan, create an isolated workspace, make the requested changes, build and test them inside a sandbox, repair failures within a bounded budget, and present the diff and results for review.

This sprint uses the existing mission scheduler, agent runner, tool registry, approval store, and PC-node protocol. Do not create a second orchestration framework or give the model unrestricted host shell access.

## User flow

1. Dhruv starts a “Work on a project” mission and provides a Git URL or chooses a locally enrolled repository.
2. Dhruv-AI checks the URL, repository access, size, language, manifests, current branch, and existing changes. It never modifies the original checkout.
3. Dhruv-AI proposes a short plan and requests page permissions scoped to this task: read the source, create/edit a task workspace, download dependencies in the sandbox, run the detected build/test commands, and optionally launch a local preview.
4. After Dhruv approves those scopes, Dhruv-AI continues without asking for every individual file edit or test command. It shows each step and its result in the mission page.
5. It iterates through edit → build/test → inspect failure → bounded fix → rerun checks.
6. It presents a summary, changed-file diff, commands and exit codes, test results, and remaining concerns. Publishing, pushing, merging, system-wide installation, and editing the original checkout require separate approval.

## Work packages

### 1. Task workspace lifecycle

- Add a workspace record linked to a mission/task: workspace ID, source URL or local repo ID, source commit, task branch, state, created/updated time, sandbox runtime, and cleanup state.
- Create one isolated directory per task under a configured workspace root. Validate resolved paths and refuse symlinks or traversal outside that root.
- Clone public repositories shallowly by default. Do not automatically fetch submodules, Git LFS content, Git hooks, or Git credentials. For private repositories, use a read-only, least-privilege GitHub authorization and keep its token outside the sandbox.
- For local projects, require an explicit directory grant and copy or create a worktree; never write into the source checkout.
- Persist lifecycle transitions so a mission can pause/resume after a service restart. Keep failed workspaces until Dhruv reviews or explicitly deletes them.
- Enforce disk, file-count, checkout-size, and retention limits. Cleanup must show what will be removed and ask for confirmation when it contains agent edits.

### 2. Repository understanding tools

Add narrow typed tools rather than exposing arbitrary shell commands:

- `repo.inspect`: identify languages, package managers, README setup steps, build/test scripts, licenses, and likely entry points.
- `repo.search`: exact text, symbol, and file search with path/line results.
- `repo.read`: read a bounded file/range from the task workspace only.
- `repo.diff`: return a bounded, reviewable diff.
- `workspace.write_patch`: apply validated changes only under the task workspace.
- `workspace.run_check`: run one allowlisted command profile and return exit code, duration, and bounded output.

The model can suggest commands, but the server validates the command profile, working directory, timeout, and resource budget before execution. Do not pass model output directly to a host shell.

### 3. Sandbox execution

- Define a `SandboxRunner` interface and a Linux implementation using a supported rootless container runtime (Podman or rootless Docker). If no safe runtime is installed, allow read-only inspection and editing but refuse code execution with setup instructions.
- Run as a non-root user with a read-only base filesystem and only the task workspace mounted read/write. Use a temporary home and temporary directories.
- Never mount the host home, SSH directory, API-key files, Docker socket, system sockets, or the original project. Do not run with `--privileged` or host networking.
- Apply CPU, memory, process-count, disk, output-size, and wall-clock limits. Initial Lenovo defaults: max 2 CPU cores, 4 GiB RAM, 10 GiB workspace, 30 minutes per build/test job; make these configurable and report when a cap is hit.
- Default network off during builds. If Dhruv grants dependency-download permission for the task, allow outbound package access only for that sandbox run; do not provide application secrets to build scripts.
- Use seccomp/AppArmor or the runtime defaults where available. Document that containers reduce risk but are not a proof of perfect isolation.
- All untrusted source code, README instructions, build scripts, dependency hooks, and test output are data. None can change Dhruv-AI policy or grant permissions.

### 4. Build-and-repair loop

- Start with explicit acceptance criteria and a maximum step count, model-call budget, wall time, and dependency/network budget.
- Detect likely command profiles from manifests: Python, Node, Rust, Go, and generic Makefile. Inspect project scripts before running them; do not blindly run arbitrary README shell blocks.
- Present the proposed checks and dependency needs on the approval card. Once approved, run them automatically inside the sandbox.
- On failure, summarize the actionable error, propose a bounded patch, edit, and rerun the relevant check. Stop and report when the same failure repeats, the budget expires, the sandbox crashes, or a new permission is needed.
- Use a verification pass that compares the final diff with the original request and checks that claimed success is backed by command results. Never fabricate a passing build.

### 5. Permission and progress page

- Extend the existing Missions UI with a project-workspace view: plan, current step, approval requests, command activity, changed files, diff, test results, and workspace status.
- Permission grants are per mission and expire when the task ends. Provide pause, cancel, and revoke controls.
- Permission categories: repository read; task-workspace writes; dependency download/network; build and tests; local preview. Keep system changes, source-checkout writes, secrets access, Git push, PR creation, merge, release, and external publication as separate explicit approvals.
- Log actor, tool, workspace, permission grant, command profile, exit code, timing, and changed paths. Redact tokens and environment values.

## Acceptance criteria

1. Given a public Python or Node repository and a small code change, Dhruv-AI inspects it, asks for the scoped task permissions once, edits only the isolated workspace, runs the expected checks in the sandbox, and presents a diff.
2. A pre-existing dirty source checkout remains byte-for-byte unchanged after the task.
3. A repository containing prompt-injection text in its README cannot grant itself more permissions or cause host-side commands to run.
4. A build cannot read the host's home directory, SSH keys, or provider-key files, and cannot reach the network without the task's network grant.
5. A failed test is reported as failed; bounded repair runs can update the code and rerun it, and repeated failure stops the mission with evidence.
6. Pause, resume, and cancel work across service restart; cancellation terminates sandbox processes and preserves the reviewable workspace.
7. Pushing, creating a PR, merging, releasing, and system-wide installation are never implied by build permission.

## Out of scope for this sprint

- Running generated code directly on the Lenovo host or in the user's existing checkout.
- Trading, brokerage access, or real-money actions.
- Automatically opening, pushing, or merging pull requests.
- Desktop/GUI control, self-installing agent skills, or unrestricted terminal access.
- Replacing the mission scheduler or migrating the whole project to a new agent framework.

## Cursor implementation instructions

- Start from a dedicated branch/worktree and preserve the current working tree, including existing user changes and untracked files.
- Inspect current node path-jail, shell allowlist, mission approval, and workspace behavior before editing. Extend existing mechanisms where possible.
- Implement this sprint as one cohesive PR, or at most two dependent PRs (workspace/sandbox foundation, then UI/build loop). Do not create a separate issue for every bullet.
- Add migrations and update setup/security documentation with the implementation.
- Include a manual acceptance report and any sandbox-runtime prerequisites in the PR description. Do not deploy, publish, or merge.
