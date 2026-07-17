"""Export ZenHub workspace data via the GraphQL API.

ZenHub stores overlay metadata on top of GitHub issues plus its own entities.
Issue titles/bodies/comments largely live in GitHub, but pipeline placement,
board ordering, estimates, priorities, sprints, epics, roadmap and workspace
settings are ZenHub-only and are lost when a workspace is deleted. This script
captures all of that so the setup can be rebuilt later if needed.

Captured per workspace:
  - settings (description, privacy, estimate defaults, sprint config, default repo)
  - repositories and pipeline definitions (stage, WIP limits, stale config)
  - priorities, sprints, epics, releases, roadmap catalogues
  - issues keyed by owner/repo#number, each with pipeline + relativePosition,
    estimate, priority, sprints, issue type, milestone, labels, assignees,
    parent/child links, blocking/blocked, connected PRs and body

Read-only: no mutations are performed.

Usage:
    # ZENHUB_TOKEN is read from the environment or a local .env file
    uv run zenhub_export.py --list                 # discover workspace IDs
    uv run zenhub_export.py WORKSPACE_ID [...]      # export named workspaces
    uv run zenhub_export.py --all                   # export every workspace found
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.zenhub.com/public/graphql"
PAGE_SIZE = 100
MAX_RETRIES = 4


class ZenHubClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        for attempt in range(MAX_RETRIES):
            resp = self._client.post("", json=payload)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2**attempt
                print(f"  rate/server error {resp.status_code}; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("errors"):
                raise RuntimeError(json.dumps(body["errors"], indent=2))
            return body["data"]
        raise RuntimeError(f"exhausted retries for query: {variables}")


ORG_WORKSPACES = """
query OrgWorkspaces($orgId: ID!, $cursor: String) {
  node(id: $orgId) {
    ... on ZenhubOrganization {
      workspaces(first: 25, after: $cursor) {
        nodes { id name }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ORGS = """
query Orgs($cursor: String) {
  viewer {
    zenhubOrganizations(first: 50, after: $cursor) {
      nodes { id name }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

WORKSPACE_META = """
query WorkspaceMeta($workspaceId: ID!) {
  workspace(id: $workspaceId) {
    id
    name
    description
    private
    assumeEstimates
    assumedEstimateValue
    createdAt
    updatedAt
    defaultRepository { id ghId name ownerName }
    sprintConfig {
      id name kind period startDay endDay tzIdentifier
      settings { autoAddToSprintOnPipelineMove moveUnfinishedIssues withTime }
    }
    repositoriesConnection(first: 100) {
      nodes { id ghId name ownerName isPrivate isArchived }
    }
    pipelinesConnection(first: 50) {
      nodes {
        id
        name
        description
        stage
        pipelineConfiguration {
          staleIssues
          staleInterval
          showAgeInPipeline
          wipLimits { nodes { id blockPipeline limitValue } }
        }
      }
    }
  }
}
"""

WORKSPACE_PRIORITIES = """
query Priorities($workspaceId: ID!, $cursor: String) {
  workspace(id: $workspaceId) {
    prioritiesConnection(first: 100, after: $cursor) {
      nodes { id name color description }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

WORKSPACE_SPRINTS = """
query Sprints($workspaceId: ID!, $cursor: String) {
  workspace(id: $workspaceId) {
    sprints(first: 50, after: $cursor) {
      nodes {
        id name generatedName state
        startAt endAt totalPoints completedPoints
        closedIssuesCount createdAt
        issues(first: 100) { nodes { number repository { ghId } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

WORKSPACE_EPICS = """
query Epics($workspaceId: ID!, $cursor: String) {
  workspace(id: $workspaceId) {
    zenhubEpics(first: 25, after: $cursor) {
      nodes {
        id title state body
        startOn endOn createdAt updatedAt
        estimate { value }
        labels(first: 50) { nodes { name } }
        assignees(first: 20) { nodes { githubUser { login } } }
        childIssues(first: 100, workspaceId: $workspaceId) {
          nodes { number repository { ghId } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

WORKSPACE_RELEASES = """
query Releases($workspaceId: ID!, $cursor: String) {
  workspace(id: $workspaceId) {
    releases(first: 50, after: $cursor) {
      nodes {
        id title description state
        startOn endOn createdAt closedAt
        issues(first: 100) { nodes { number repository { ghId } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

WORKSPACE_ROADMAP = """
query Roadmap($workspaceId: ID!) {
  workspace(id: $workspaceId) {
    roadmap {
      id
      keyDates(first: 100) { nodes { id date color description } }
      items(first: 100) {
        nodes {
          __typename
          ... on ZenhubEpic { id title state startOn endOn }
          ... on Project { id name state startOn endOn }
          ... on Epic { id startOn endOn issue { number repository { ghId } } }
        }
      }
    }
  }
}
"""

ISSUE_FIELDS = """
fragment IssueFields on Issue {
  id
  ghNodeId
  number
  title
  state
  body
  htmlUrl
  createdAt
  closedAt
  repository { id ghId name ownerName }
  labels(first: 50) { nodes { name } }
  assignees(first: 20) { nodes { login } }
  estimate { value }
  pipelineIssue(workspaceId: $workspaceId) {
    priority { id name color }
    pipeline { id name }
    relativePosition
  }
  sprints(first: 20) { nodes { id name } }
  milestone { id title number }
  issueType {
    ... on GithubIssueType { id name }
    ... on ZenhubIssueType { id name }
  }
  githubParentIssue { number repository { ghId } }
  parentZenhubEpics(first: 10) { nodes { id title } }
  blockingIssues(first: 20) { nodes { number repository { ghId } } }
  blockedIssues(first: 20) { nodes { number repository { ghId } } }
  connectedPrs(first: 20) { nodes { number repository { ghId } } }
}
"""

PIPELINE_ISSUES = ISSUE_FIELDS + """
query PipelineIssues($pipelineId: ID!, $workspaceId: ID!, $cursor: String) {
  searchIssuesByPipeline(pipelineId: $pipelineId, filters: {}, first: 100, after: $cursor) {
    nodes { ...IssueFields }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CLOSED_ISSUES = ISSUE_FIELDS + """
query ClosedIssues($workspaceId: ID!, $cursor: String) {
  searchClosedIssues(workspaceId: $workspaceId, filters: {}, first: 100, after: $cursor) {
    nodes { ...IssueFields }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def discover_workspaces(client: ZenHubClient) -> list[dict[str, Any]]:
    orgs: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        conn = client.query(ORGS, {"cursor": cursor})["viewer"]["zenhubOrganizations"]
        orgs.extend(conn["nodes"])
        info = conn["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]

    workspaces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for org in orgs:
        ws_cursor: str | None = None
        while True:
            conn = client.query(ORG_WORKSPACES, {"orgId": org["id"], "cursor": ws_cursor})["node"]["workspaces"]
            for ws in conn["nodes"]:
                if ws["id"] in seen:
                    continue
                seen.add(ws["id"])
                workspaces.append({"id": ws["id"], "name": ws["name"], "organization": org["name"]})
            info = conn["pageInfo"]
            if not info["hasNextPage"]:
                break
            ws_cursor = info["endCursor"]
    return workspaces


def paginate(client: ZenHubClient, query: str, variables: dict[str, Any], root: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = client.query(query, {**variables, "cursor": cursor})
        conn = data[root]
        nodes.extend(conn["nodes"])
        info = conn["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
    return nodes


def paginate_workspace(client: ZenHubClient, query: str, workspace_id: str, connection: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = client.query(query, {"workspaceId": workspace_id, "cursor": cursor})
        conn = data["workspace"][connection]
        nodes.extend(conn["nodes"])
        info = conn["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
    return nodes


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def issue_key(issue: dict[str, Any]) -> str:
    repo = issue.get("repository") or {}
    return f"{repo.get('ownerName')}/{repo.get('name')}#{issue.get('number')}"


def export_workspace(client: ZenHubClient, workspace_id: str) -> dict[str, Any]:
    meta = client.query(WORKSPACE_META, {"workspaceId": workspace_id})["workspace"]
    name = meta["name"]
    repositories = meta["repositoriesConnection"]["nodes"]
    pipelines = meta["pipelinesConnection"]["nodes"]
    settings = {
        "description": meta.get("description"),
        "private": meta.get("private"),
        "assumeEstimates": meta.get("assumeEstimates"),
        "assumedEstimateValue": meta.get("assumedEstimateValue"),
        "createdAt": meta.get("createdAt"),
        "updatedAt": meta.get("updatedAt"),
        "defaultRepository": meta.get("defaultRepository"),
        "sprintConfig": meta.get("sprintConfig"),
    }
    print(f"Workspace {name} ({workspace_id}): {len(pipelines)} pipelines, {len(repositories)} repos")

    issues_by_key: dict[str, dict[str, Any]] = {}

    for pipeline in pipelines:
        try:
            nodes = paginate(
                client,
                PIPELINE_ISSUES,
                {"pipelineId": pipeline["id"], "workspaceId": workspace_id},
                "searchIssuesByPipeline",
            )
        except RuntimeError as exc:
            print(f"  skipping pipeline {pipeline['name']} (likely closed): {exc}", file=sys.stderr)
            continue
        for issue in nodes:
            issues_by_key[issue_key(issue)] = issue
        print(f"  pipeline {pipeline['name']}: {len(nodes)} open issues")

    closed = paginate(client, CLOSED_ISSUES, {"workspaceId": workspace_id}, "searchClosedIssues")
    for issue in closed:
        issues_by_key.setdefault(issue_key(issue), issue)
    print(f"  closed: {len(closed)} issues")

    priorities = paginate_workspace(client, WORKSPACE_PRIORITIES, workspace_id, "prioritiesConnection")
    sprints = paginate_workspace(client, WORKSPACE_SPRINTS, workspace_id, "sprints")
    epics = paginate_workspace(client, WORKSPACE_EPICS, workspace_id, "zenhubEpics")
    releases = paginate_workspace(client, WORKSPACE_RELEASES, workspace_id, "releases")
    roadmap = client.query(WORKSPACE_ROADMAP, {"workspaceId": workspace_id})["workspace"]["roadmap"]
    print(
        f"  catalogues: {len(priorities)} priorities, {len(sprints)} sprints, "
        f"{len(epics)} epics, {len(releases)} releases"
    )

    return {
        "workspace_id": workspace_id,
        "name": name,
        "settings": settings,
        "repositories": repositories,
        "pipelines": pipelines,
        "priorities": priorities,
        "sprints": sprints,
        "epics": epics,
        "releases": releases,
        "roadmap": roadmap,
        "issues": issues_by_key,
    }


def write_output(out_dir: Path, workspaces_meta: list[dict[str, Any]], exports: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "workspaces.json").write_text(json.dumps(workspaces_meta, indent=2))
    for export in exports:
        safe = export["workspace_id"]
        (out_dir / f"workspace-{safe}.json").write_text(json.dumps(export, indent=2))
    print(f"\nWrote {len(exports)} workspace export(s) to {out_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export ZenHub workspace overlay data.")
    parser.add_argument("workspace_ids", nargs="*", help="Workspace IDs to export")
    parser.add_argument("--list", action="store_true", help="List discoverable workspaces and exit")
    parser.add_argument("--all", action="store_true", help="Export every discoverable workspace")
    parser.add_argument("--out", default=None, help="Output directory (default: exports/<timestamp>)")
    args = parser.parse_args()

    load_dotenv()
    token = os.environ.get("ZENHUB_TOKEN")
    if not token:
        print("ZENHUB_TOKEN not set (checked environment and .env)", file=sys.stderr)
        return 2

    client = ZenHubClient(token)
    try:
        workspaces_meta = discover_workspaces(client)

        if args.list:
            for ws in workspaces_meta:
                print(f"{ws['id']}  {ws['name']}  [{ws['organization']}]")
            return 0

        if args.all:
            targets = [ws["id"] for ws in workspaces_meta]
        else:
            targets = args.workspace_ids

        if not targets:
            parser.error("provide workspace IDs, --all, or --list")

        exports = [export_workspace(client, wid) for wid in targets]

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(args.out) if args.out else Path("exports") / stamp
        write_output(out_dir, workspaces_meta, exports)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
