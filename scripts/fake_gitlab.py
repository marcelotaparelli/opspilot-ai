"""Minimal fake of the GitLab REST v4 issues API, for tests and the local smoke stack only.

Implements POST/GET /api/v4/projects/:id/issues, GET/PUT .../issues/:iid (read, close) with
PRIVATE-TOKEN auth plus a test-only
/_control API for fault injection. Faults model what matters for idempotency:
  status:<code>          answer <code> without creating anything
  drop_after_create      create the issue, then close the socket without a response
  hold_after_create:<s>  create the issue, then wait <s> seconds before answering
  malformed_after_create create the issue, then answer 201 with an invalid body
Faults apply to the next N create requests (default 1), then revert to normal.
Not a GitLab emulator: search is a plain case-insensitive substring match.
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

ISSUES = re.compile(r"^/api/v4/projects/(\d+)/issues$")
ISSUE = re.compile(r"^/api/v4/projects/(\d+)/issues/(\d+)$")


class State:
    def __init__(self, token: str, projects: set[int]) -> None:
        self.token = token
        self.projects = projects
        self.lock = threading.Lock()
        self.issues: list[dict[str, Any]] = []
        self.faults: list[str] = []
        self.creates_received = 0

    def reset(self) -> None:
        with self.lock:
            self.issues.clear()
            self.faults.clear()
            self.creates_received = 0


class Handler(BaseHTTPRequestHandler):
    server: "FakeGitLab"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        return  # never echo request lines (they could contain tokens in real deployments)

    def reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def authorised(self) -> bool:
        if self.headers.get("PRIVATE-TOKEN") != self.server.state.token:
            self.reply(401, {"message": "401 Unauthorized"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        state = self.server.state
        url = urlsplit(self.path)
        if url.path == "/_control/issues":
            with state.lock:
                self.reply(
                    200, {"issues": state.issues, "creates_received": state.creates_received}
                )
            return
        single = ISSUE.match(url.path)
        if single:
            if self.authorised():
                issue = self.find(int(single.group(1)), int(single.group(2)))
                self.reply(200, issue) if issue else self.reply(404, {"message": "404 Not found"})
            return
        match = ISSUES.match(url.path)
        if not match or not self.authorised():
            if not match:
                self.reply(404, {"message": "404 Not Found"})
            return
        project = int(match.group(1))
        search = parse_qs(url.query).get("search", [""])[0].casefold()
        with state.lock:
            found = [
                issue
                for issue in state.issues
                if issue["project_id"] == project and search in issue["description"].casefold()
            ]
        self.reply(200, found)

    def find(self, project: int, iid: int) -> dict[str, Any] | None:
        with self.server.state.lock:
            return next(
                (
                    i
                    for i in self.server.state.issues
                    if (i["project_id"], i["iid"]) == (project, iid)
                ),
                None,
            )

    def do_PUT(self) -> None:  # noqa: N802 - stdlib name
        single = ISSUE.match(urlsplit(self.path).path)
        if not single:
            self.reply(404, {"message": "404 Not Found"})
            return
        if not self.authorised():
            return
        payload = self.body()
        issue = self.find(int(single.group(1)), int(single.group(2)))
        if issue is None:
            self.reply(404, {"message": "404 Not found"})
            return
        with self.server.state.lock:
            if payload.get("state_event") == "close":
                issue["state"] = "closed"
            self.reply(200, issue)

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        state = self.server.state
        url = urlsplit(self.path)
        if url.path == "/_control/reset":
            state.reset()
            self.reply(200, {})
            return
        if url.path == "/_control/faults":
            payload = self.body()
            with state.lock:
                state.faults = [payload["fault"]] * int(payload.get("times", 1))
            self.reply(200, {})
            return
        match = ISSUES.match(url.path)
        if not match:
            self.reply(404, {"message": "404 Not Found"})
            return
        if not self.authorised():
            return
        project = int(match.group(1))
        payload = self.body()
        with state.lock:
            state.creates_received += 1
            fault = state.faults.pop(0) if state.faults else "none"
        if fault.startswith("status:"):
            self.reply(int(fault.split(":")[1]), {"message": "injected"})
            return
        if project not in state.projects:
            self.reply(404, {"message": "404 Project Not Found"})
            return
        if not isinstance(payload.get("title"), str) or not payload["title"].strip():
            self.reply(400, {"message": {"title": ["can't be blank"]}})
            return
        with state.lock:
            iid = 1 + sum(1 for issue in state.issues if issue["project_id"] == project)
            issue = {
                "id": 1000 + len(state.issues) + 1,
                "iid": iid,
                "project_id": project,
                "title": payload["title"],
                "description": payload.get("description") or "",
                "labels": [label for label in (payload.get("labels") or "").split(",") if label],
                "assignee_ids": payload.get("assignee_ids") or [],
                "state": "opened",
                "web_url": f"http://gitlab.invalid/project/{project}/-/issues/{iid}",
            }
            state.issues.append(issue)
        if fault == "drop_after_create":
            self.close_connection = True
            self.connection.close()
            return
        if fault.startswith("hold_after_create:"):
            time.sleep(float(fault.split(":")[1]))
        if fault == "malformed_after_create":
            self.reply(201, {"unexpected": True})
            return
        self.reply(201, issue)


class FakeGitLab(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: State) -> None:
        super().__init__(address, Handler)
        self.state = state


def serve_in_thread(token: str, projects: set[int]) -> tuple[FakeGitLab, str]:
    server = FakeGitLab(("127.0.0.1", 0), State(token, projects))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", required=True)
    parser.add_argument("--projects", default="101")
    args = parser.parse_args()
    projects = {int(item) for item in args.projects.split(",")}
    FakeGitLab((args.host, args.port), State(args.token, projects)).serve_forever()


if __name__ == "__main__":
    main()
