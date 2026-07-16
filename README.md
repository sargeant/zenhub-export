# zenhub-export

Read-only exporter for ZenHub workspace data, via the ZenHub GraphQL API.

We trialled several ZenHub workspaces and want to delete them and start clean.
This tool saves everything ZenHub-specific so a workspace could be rebuilt later
if needed. It performs **no mutations**.

## Why this exists (what ZenHub actually holds)

Most issue content  titles, bodies, comments, open/closed state  lives in
**GitHub** and survives a workspace deletion. ZenHub stores an *overlay* plus a
few of its own entities. Those are what disappear when you delete a workspace,
so those are what we capture:

- Board placement and vertical ordering of issues within pipelines
- Estimates, priorities, sprints
- Issue types and epic / sub-issue hierarchy
- Pipeline definitions, workspace settings, roadmap, releases

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

1. Generate a **Personal API Key** in ZenHub: click your username (bottom-left)
   ’ *Account management* ’ *API* section ’ create key. It is shown once.
2. Put it in a `.env` file in this directory:

   ```
   ZENHUB_TOKEN=your_key_here
   ```

   (`ZENHUB_TOKEN` is read from the environment or `.env`.)

## Usage

```bash
uv run zenhub_export.py --list              # discover workspace IDs
uv run zenhub_export.py WORKSPACE_ID [...]  # export one or more workspaces
uv run zenhub_export.py --all               # export every workspace you can see
uv run zenhub_export.py --all --out backup  # custom output directory
```

Output lands in `exports/<UTC-timestamp>/` by default.

The workspace ID is also visible in a board URL, e.g.
`app.zenhub.com/workspaces/name-<ID>/board`.

## Output layout

```
exports/20260716T214139Z/
  workspaces.json                 # index: id, name, organization for all workspaces
  workspace-<id>.json             # one file per exported workspace
```

### `workspaces.json`

A flat list used for discovery:

```json
[
  { "id": "691cc4d8...", "name": "RPC Modernization", "organization": "ietf-tools" }
]
```

### `workspace-<id>.json`

Top-level keys:

| Key | Contents |
|-----|----------|
| `workspace_id`, `name` | Identifiers |
| `settings` | description, privacy, estimate defaults, `sprintConfig`, `defaultRepository`, timestamps |
| `repositories` | repos attached to the workspace (`ghId`, `name`, `ownerName`, privacy/archived flags) |
| `pipelines` | board columns: `name`, `description`, `stage`, `pipelineConfiguration` (WIP limits, stale config) |
| `priorities` | priority definitions (`name`, `color`, `description`) |
| `sprints` | sprint definitions: dates, state, points, member issues |
| `epics` | ZenHub epic entities: title, body, dates, estimate, child issues |
| `releases` | release definitions and member issues (if used) |
| `roadmap` | `keyDates` and roadmap `items` (epics / projects) |
| `issues` | map keyed by `owner/repo#number` (see below) |

### Issue records

Each entry in `issues` is keyed `owner/repo#number` (e.g.
`ietf-tools/rpc-modernization#42`) and contains:

| Field | Notes |
|-------|-------|
| `number`, `title`, `state`, `htmlUrl` | identity (also in GitHub) |
| `ghNodeId` | GitHub GraphQL node ID; **null in some older workspaces** |
| `body` | populated for ZenHub-native issues; GitHub issues carry theirs in GitHub |
| `repository` | `{ ghId, name, ownerName }`  the reliable join key |
| `pipelineIssue` | `pipeline`, `priority`, and `relativePosition` (board order) |
| `estimate` | `{ value }` |
| `sprints` | sprints the issue belongs to |
| `milestone` | GitHub milestone (`title`, `number`) |
| `issueType` | Epic / Project / etc. under the current issue-type model |
| `githubParentIssue` | sub-issue parent (`number` + repo `ghId`) |
| `parentZenhubEpics` | legacy epic links |
| `blockingIssues`, `blockedIssues` | dependency graph |
| `connectedPrs` | linked pull requests |

## Known limitations

These are API/data facts, not bugs:

- **Board ordering (`relativePosition`) exists only for issues in open
  pipelines.** Closed issues aren't on the board, so they have no position.
- **`ghNodeId` is null in some older workspaces.** Use `repository.ghId` +
  `number` as the stable key  it is present everywhere.
- **Comments and activity history are not exported**  they live in GitHub and
  survive workspace deletion.
- Epics moved to the *issue type / sub-issue* model (ZenHub, June 2025).
  `issueType` and `githubParentIssue` reflect the current hierarchy;
  `parentZenhubEpics` / `epics` cover any legacy epics.

## Rehydration notes

There is no import script  this is export only. To rebuild later, the ZenHub
GraphQL API offers matching mutations (create workspace, pipelines, sprints,
priorities; set estimate; move issue to pipeline; assign sprint; set issue
type / parent). Match saved records back to GitHub issues via
`repository.ghId` + `number`.
