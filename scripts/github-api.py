"""GitHub control-plane requests for a single explicit repository/run."""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(path, method="GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request("https://api.github.com" + path, data=body, method=method, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "runs-on-ami-example",
    })
    with urlopen(req, timeout=30) as response:
        value = response.read()
        return json.loads(value) if value else {}


def jobs(repository, run_id, attempt):
    result = []
    page = 1
    while True:
        value = request(f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")["jobs"]
        result.extend(value)
        if len(value) < 100:
            return result
        page += 1


def cancel(repository, run_id):
    try:
        request(f"/repos/{repository}/actions/runs/{run_id}/cancel", "POST")
    except HTTPError as error:
        if error.code != 409:  # Already completed/cancelled is an idempotent outcome.
            raise
