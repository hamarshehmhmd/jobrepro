# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line interface for jobrepro."""
import argparse
import json
import os
import sys

from . import __version__
from .expr import to_str
from .fromrun import command_for, plan_from_run
from .runner import JobRunner, Options, parse_dotenv
from .workflow import (
    WorkflowError,
    expand_matrix,
    find_workflows,
    job_display_name,
    load_yaml,
    resolve_target,
    step_display_name,
)

EPILOG = """examples:
  jobrepro list                                   jobs and their matrix combinations
  jobrepro run ci.yml:test --matrix python=3.12   run one job's steps here
  jobrepro run test --only "Run tests"            just one step (by number, id or name)
  jobrepro run test --break 4                     open a shell right before step 4
  jobrepro run test --break-on-failure            shell in the failing step's env, then retry
  jobrepro run test --image ubuntu:24.04          run the steps inside a container
  jobrepro run test --dry-run                     print the rendered scripts, run nothing
  jobrepro from-run https://github.com/o/r/actions/runs/123   failed run -> local command
"""


def _kv(items, what):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise WorkflowError("%s expects KEY=VALUE, got %r" % (what, it))
        k, v = it.split("=", 1)
        out[k] = v
    return out


def add_run_args(r):
    r.add_argument("target", help="JOB, or WORKFLOW:JOB (e.g. ci.yml:test)")
    r.add_argument("-C", "--dir", default=".", help="repository root / workspace (default .)")
    r.add_argument("-m", "--matrix", action="append", metavar="KEY=VALUE", help="pick the matrix combination")
    r.add_argument("--secret", action="append", metavar="NAME=VALUE", help="provide a secret (masked in output)")
    r.add_argument("--secrets-file", metavar="FILE", help="dotenv file with secrets (NAME=VALUE lines)")
    r.add_argument("--var", action="append", metavar="NAME=VALUE", help="repository variable (vars.NAME)")
    r.add_argument("--input", action="append", metavar="NAME=VALUE", help="workflow_dispatch/workflow_call input")
    r.add_argument("--set", action="append", metavar="CTX.PATH=VALUE",
                   help="override any context value, e.g. github.ref_name=main or needs.build.outputs.tag=v1")
    r.add_argument("--event", help="github.event_name (default: push or the workflow's first trigger)")
    r.add_argument("--event-file", metavar="JSON", help="event payload for github.event")
    sel = r.add_argument_group("step selection (STEP = number, id, or part of the name)")
    sel.add_argument("--from", dest="from_step", metavar="STEP", help="start at this step (earlier steps don't run)")
    sel.add_argument("--until", dest="until_step", metavar="STEP", help="stop after this step")
    sel.add_argument("--only", action="append", metavar="STEP", help="run only these steps")
    sel.add_argument("--skip", action="append", metavar="STEP", help="skip these steps")
    dbg = r.add_argument_group("debugging")
    dbg.add_argument("-b", "--break", dest="breaks", action="append", metavar="STEP",
                     help="open an interactive shell before STEP, with its env and working directory")
    dbg.add_argument("-B", "--break-on-failure", action="store_true",
                     help="when a step fails, open a shell in its env, then retry / continue / abort")
    dbg.add_argument("-n", "--dry-run", action="store_true", help="print each step's rendered script; run nothing")
    dbg.add_argument("--debug", action="store_true", help="set RUNNER_DEBUG=1 and show ::debug:: lines")
    r.add_argument("--image", help="run steps inside this container image (needs docker); "
                                   "a job's own container: is used automatically")
    r.add_argument("--no-ci-env", action="store_true", help="don't set CI=true / GITHUB_ACTIONS=true")
    r.add_argument("--color", choices=["auto", "always", "never"], default="auto")



def build_parser():
    p = argparse.ArgumentParser(
        prog="jobrepro",
        description="Run one GitHub Actions job, or one step of it, on your machine: same env, expressions, "
                    "GITHUB_ENV/OUTPUT/PATH and matrix values, with breakpoints. No push-and-wait.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="jobrepro " + __version__)
    sub = p.add_subparsers(dest="cmd")

    ls = sub.add_parser("list", help="list workflows, jobs, matrix combinations and steps")
    ls.add_argument("-C", "--dir", default=".", help="repository root (default .)")
    ls.add_argument("-s", "--steps", action="store_true", help="also list each job's steps")

    r = sub.add_parser("run", help="run a job's steps locally", epilog=EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    add_run_args(r)
    pl = sub.add_parser("plan", help="same as `run --dry-run`: print the rendered steps")
    add_run_args(pl)

    fr = sub.add_parser("from-run", help="turn a failed run URL into the local jobrepro command (uses gh)")
    fr.add_argument("run", help="run URL (…/actions/runs/ID or …/actions/runs/ID/job/JOBID) or a run id")
    fr.add_argument("--repo", help="OWNER/NAME when passing a bare run id")
    fr.add_argument("-C", "--dir", default=".", help="local checkout (default .)")
    fr.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def cmd_list(a):
    wfs = find_workflows(a.dir)
    if not wfs:
        print("no workflows in %s" % os.path.join(a.dir, ".github", "workflows"))
        return 1
    for path in wfs:
        wf = load_yaml(path)
        print("%s  %s" % (os.path.relpath(path, a.dir), to_str(wf.get("name") or "")))
        for jid, job in (wf.get("jobs") or {}).items():
            job = job or {}
            matrix = (job.get("strategy") or {}).get("matrix")
            try:
                combos = expand_matrix(matrix, {"matrix": {}, "github": {}, "inputs": {}, "vars": {}, "needs": {}})
                mtxt = "%d matrix combination%s" % (len(combos), "" if len(combos) == 1 else "s") if matrix else ""
            except Exception:
                combos, mtxt = [{}], "matrix from an expression"
            kind = "reusable workflow" if "uses" in job else "%d steps" % len(job.get("steps") or [])
            print("  %-24s %-40s %s%s" % (jid, job_display_name(jid, job, combos[0] if matrix else {}, {"matrix": combos[0]})
                                         [:40], kind, ("  · " + mtxt) if mtxt else ""))
            if matrix and len(combos) > 1:
                for cmb in combos[:12]:
                    print("      --matrix " + " --matrix ".join("%s=%s" % (k, to_str(v)) for k, v in cmb.items()
                                                                  if not isinstance(v, (dict, list))))
                if len(combos) > 12:
                    print("      … %d more" % (len(combos) - 12))
            if a.steps:
                for i, s in enumerate(job.get("steps") or [], 1):
                    sid = ("  [id: %s]" % s["id"]) if s.get("id") else ""
                    print("      %2d. %s%s" % (i, step_display_name(s)[:70], sid))
    return 0


def _color(choice):
    if choice == "always":
        return True
    if choice == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))


def cmd_run(a, dry=False):
    path, job = resolve_target(a.target, a.dir)
    secrets = {}
    if getattr(a, "secrets_file", None):
        secrets.update(parse_dotenv(a.secrets_file))
    secrets.update(_kv(getattr(a, "secret", None), "--secret"))
    opts = Options(
        matrix=_kv(a.matrix, "--matrix"), secrets=secrets, vars=_kv(getattr(a, "var", None), "--var"),
        inputs=_kv(a.input, "--input"), sets=_kv(a.set, "--set"), event=a.event,
        event_file=getattr(a, "event_file", None), from_step=getattr(a, "from_step", None),
        until_step=getattr(a, "until_step", None), only=getattr(a, "only", None) or [],
        skip=getattr(a, "skip", None) or [], breaks=getattr(a, "breaks", None) or [],
        break_on_failure=getattr(a, "break_on_failure", False), dry_run=dry or getattr(a, "dry_run", False),
        image=getattr(a, "image", None), ci_env=not getattr(a, "no_ci_env", False), color=_color(a.color),
        workspace=a.dir, debug=getattr(a, "debug", False),
    )
    res = JobRunner(path, job, opts).run()
    return 1 if res.failed else 0


def cmd_from_run(a):
    plan = plan_from_run(a.run, a.repo, a.dir)
    cmd = command_for(plan)
    if a.json:
        out = {k: v for k, v in plan.items() if k != "workflow_text"}
        out["command"] = cmd
        print(json.dumps(out, indent=2))
        return 0
    print("run      %s" % plan["run_url"])
    print("         %s  (%s on %s @ %s)" % (plan["title"], plan["event"], plan["branch"], plan["sha"][:10]))
    print("job      %s  ->  %s:%s" % (plan["job_name"], plan["workflow_path"], plan["job_id"]))
    if plan["matrix"]:
        print("matrix   %s" % ", ".join("%s=%s" % (k, to_str(v)) for k, v in plan["matrix"].items()))
    if plan["failed_step_name"]:
        where = ("step %s" % plan["failed_step"]) if plan["failed_step"] else "not found in the workflow"
        print("failed   %s  (%s)" % (plan["failed_step_name"], where))
    print("runs-on  %s" % plan["runs_on"])
    print("")
    if plan["local_head"] != plan["sha"]:
        print("# your checkout is not at the failing commit; get a clean copy of it:")
        print("git fetch origin %s && git worktree add ../%s-%s %s && cd ../%s-%s" % (
            plan["sha"], plan["repo"].split("/")[1], plan["sha"][:8], plan["sha"],
            plan["repo"].split("/")[1], plan["sha"][:8]))
    print("# re-run the job up to the failed step; a failure opens a shell in that step's env:")
    print(cmd)
    return 0


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        if a.cmd == "list":
            return cmd_list(a)
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "plan":
            return cmd_run(a, dry=True)
        if a.cmd == "from-run":
            return cmd_from_run(a)
    except WorkflowError as e:
        print("jobrepro: error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\njobrepro: interrupted", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
