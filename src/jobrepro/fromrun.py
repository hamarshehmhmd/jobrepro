# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a failed GitHub Actions run URL into the exact local `jobrepro run` command."""
import base64
import json
import os
import re
import shlex
import subprocess

from .expr import to_str
from .workflow import (
    WorkflowError,
    expand_matrix,
    job_display_name,
    load_yaml_text,
    step_display_name,
)

URL_RE = re.compile(
    r"github\.com/(?P<repo>[^/]+/[^/]+)/actions/runs/(?P<run>\d+)(?:/attempts/\d+)?(?:/job(?:s)?/(?P<job>\d+))?"
)


def parse_ref(ref, repo=None):
    m = URL_RE.search(ref)
    if m:
        return m.group("repo"), int(m.group("run")), (int(m.group("job")) if m.group("job") else None)
    if ref.isdigit():
        if not repo:
            raise WorkflowError("a bare run id needs --repo OWNER/NAME")
        return repo, int(ref), None
    raise WorkflowError("expected a run URL like https://github.com/OWNER/REPO/actions/runs/123[/job/456]")


def gh_api(path):
    try:
        p = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except FileNotFoundError:
        raise WorkflowError("from-run needs the GitHub CLI (gh) on PATH, logged in with `gh auth login`")
    if p.returncode:
        raise WorkflowError("gh api %s failed: %s" % (path, (p.stderr or p.stdout).strip()[:300]))
    return json.loads(p.stdout)


def _ctx_for(combo):
    return {"matrix": combo, "github": {}, "inputs": {}, "vars": {}, "env": {}, "needs": {}, "strategy": {}}


def match_job(wf, display_name):
    """Find (job_id, matrix combo) whose GitHub display name equals *display_name*."""
    jobs = wf.get("jobs") or {}
    seen = []
    for jid, job in jobs.items():
        job = job or {}
        matrix = (job.get("strategy") or {}).get("matrix")
        try:
            combos = expand_matrix(matrix, _ctx_for({})) if matrix is not None else [{}]
        except WorkflowError:
            combos = [{}]
        for combo in combos:
            name = job_display_name(jid, job, combo, _ctx_for(combo))
            seen.append(name)
            if name == display_name:
                return jid, combo
    # Called reusable workflows show up as "caller / callee"; match the caller part.
    head = display_name.split(" / ")[0]
    for jid, job in jobs.items():
        if head in (jid, to_str((job or {}).get("name"))):
            return jid, {}
    raise WorkflowError("no job in the workflow has the display name %r (known: %s)" % (display_name, "; ".join(seen)))


def match_step(job, step_name):
    for i, s in enumerate(job.get("steps") or [], 1):
        if step_display_name(s) == step_name:
            return i, s
    for i, s in enumerate(job.get("steps") or [], 1):
        d = step_display_name(s)
        if step_name.startswith(d) or d.startswith(step_name):
            return i, s
    return None, None


def distinguishing_keys(combo, combos):
    """Smallest (greedy) list of matrix keys whose values single out *combo*."""
    keys = [k for k, v in combo.items() if not isinstance(v, (dict, list))]
    chosen = []
    rest = [c for c in combos if c is not combo and c != combo]
    while rest and keys:
        best = max(keys, key=lambda k: sum(1 for c in rest if to_str(c.get(k)) != to_str(combo.get(k))))
        keys.remove(best)
        if not any(to_str(c.get(best)) != to_str(combo.get(best)) for c in rest):
            continue
        chosen.append(best)
        rest = [c for c in rest if to_str(c.get(best)) == to_str(combo.get(best))]
    return [k for k in combo if k in chosen]


def plan_from_run(ref, repo=None, workspace="."):
    repo, run_id, job_id = parse_ref(ref, repo)
    run = gh_api("repos/%s/actions/runs/%d" % (repo, run_id))
    jobs = gh_api("repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, run_id)).get("jobs", [])
    if job_id is not None:
        target = next((j for j in jobs if j.get("id") == job_id), None)
        if target is None:
            raise WorkflowError("job %d is not in run %d" % (job_id, run_id))
    else:
        failed = [j for j in jobs if j.get("conclusion") == "failure"]
        if not failed:
            raise WorkflowError("run %d has no failed job (conclusion: %s); pass a /job/ID URL to pick one"
                                % (run_id, run.get("conclusion")))
        target = failed[0]
    path = run.get("path", "").split("@")[0]
    sha = run.get("head_sha", "")
    local = os.path.join(workspace, path)
    text = None
    head = ""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True).stdout.strip()
    except OSError:
        pass
    if head == sha and os.path.isfile(local):
        with open(local, encoding="utf-8") as fh:
            text = fh.read()
    else:
        data = gh_api("repos/%s/contents/%s?ref=%s" % (repo, path, sha))
        text = base64.b64decode(data.get("content", "")).decode("utf-8")
    wf = load_yaml_text(text, path)
    jid, combo = match_job(wf, target.get("name", ""))
    job = (wf.get("jobs") or {}).get(jid) or {}
    matrix = (job.get("strategy") or {}).get("matrix")
    try:
        combos = expand_matrix(matrix, _ctx_for({})) if matrix is not None else [{}]
    except WorkflowError:
        combos = [combo]
    failed_step = next((s for s in target.get("steps") or [] if s.get("conclusion") == "failure"), None)
    step_no, step = (None, None)
    if failed_step:
        step_no, step = match_step(job, failed_step.get("name", ""))
    return {
        "repo": repo, "run_id": run_id, "run_url": run.get("html_url"), "title": run.get("display_title"),
        "event": run.get("event"), "branch": run.get("head_branch"), "sha": sha, "workflow_path": path,
        "job_url": target.get("html_url"), "job_name": target.get("name"), "job_id": jid, "matrix": combo,
        "matrix_keys": distinguishing_keys(combo, combos),
        "failed_step_name": failed_step.get("name") if failed_step else None, "failed_step": step_no,
        "runs_on": to_str(job.get("runs-on")), "local_head": head, "workflow_text": text,
    }


def command_for(plan):
    argv = ["jobrepro", "run", "%s:%s" % (plan["workflow_path"], plan["job_id"])]
    keys = plan.get("matrix_keys")
    if keys is None:
        keys = [k for k, v in (plan["matrix"] or {}).items() if not isinstance(v, (dict, list))]
    for k in keys:
        argv += ["--matrix", "%s=%s" % (k, to_str(plan["matrix"][k]))]
    if plan.get("event") and plan["event"] not in ("push",):
        argv += ["--event", plan["event"]]
    if plan.get("failed_step"):
        argv += ["--until", str(plan["failed_step"])]
    argv += ["--break-on-failure"]
    return " ".join(shlex.quote(a) for a in argv)
