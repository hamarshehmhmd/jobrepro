# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the steps of one GitHub Actions job on this machine (or in a container)."""
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .expr import ExprError, Status, eval_if, render, to_str
from .workflow import (
    WorkflowError,
    coerce_value,
    expand_matrix,
    job_display_name,
    load_yaml,
    step_display_name,
)

SHELLS = {
    "bash": "bash --noprofile --norc -eo pipefail {0}",
    "sh": "sh -e {0}",
    "python": "python {0}",
    "pwsh": "pwsh -command \". '{0}'\"",
    "powershell": "powershell -command \". '{0}'\"",
}
EXT = {"python": ".py", "pwsh": ".ps1", "powershell": ".ps1"}

# uses: steps that have a local meaning (or none) when you are already in a checkout.
NOOP_ACTIONS = {
    "actions/checkout": "your working tree is the checkout",
    "actions/cache": "no remote cache locally; your machine's own caches apply",
    "actions/cache/restore": "no remote cache locally",
    "actions/cache/save": "no remote cache locally",
    "actions/upload-artifact": "artifacts stay in your working tree",
    "actions/download-artifact": "no artifacts from other jobs; pass files yourself",
    "actions/github-script": None,
}


class Color:
    def __init__(self, on):
        self.on = on

    def __call__(self, code, s):
        return "\x1b[%sm%s\x1b[0m" % (code, s) if self.on else s


def _git(args, cwd):
    try:
        return subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _repo_slug(url):
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return "%s/%s" % (m.group(1), m.group(2)) if m else ""


def parse_dotenv(path):
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


def parse_command_file(path):
    """Parse a GITHUB_ENV / GITHUB_OUTPUT file: `K=V` lines and `K<<DELIM ... DELIM` blocks."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        i += 1
        if not line.strip():
            continue
        m = re.match(r"^([^=<]+)<<(.+)$", line)
        if m and ("=" not in line or line.index("<<") < line.index("=")):
            key, delim = m.group(1), m.group(2)
            buf = []
            while i < len(lines) and lines[i].rstrip("\r") != delim:
                buf.append(lines[i].rstrip("\r"))
                i += 1
            if i >= len(lines):
                raise WorkflowError("%s: missing closing delimiter %r for %s" % (os.path.basename(path), delim, key))
            i += 1
            out[key] = "\n".join(buf)
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        else:
            raise WorkflowError("%s: cannot parse line %r" % (os.path.basename(path), line))
    return out


def _set_path(ctx, dotted, value):
    parts = dotted.split(".")
    cur = ctx
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


class StepSel:
    """Selects steps by 1-based number, `id`, or case-insensitive name substring."""

    def __init__(self, spec):
        self.spec = str(spec)

    def matches(self, idx, step, display):
        s = self.spec
        if s.isdigit():
            return int(s) == idx
        if step.get("id") is not None and str(step.get("id")) == s:
            return True
        return s.lower() in display.lower()


class Options:
    def __init__(self, **kw):
        self.matrix = {}
        self.secrets = {}
        self.vars = {}
        self.inputs = {}
        self.sets = {}
        self.event = None
        self.event_file = None
        self.from_step = None
        self.until_step = None
        self.only = []
        self.skip = []
        self.breaks = []
        self.break_on_failure = False
        self.dry_run = False
        self.image = None
        self.ci_env = True
        self.color = sys.stdout.isatty()
        self.workspace = os.getcwd()
        self.debug = False
        self.out = sys.stdout
        self.inp = sys.stdin
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(k)
            setattr(self, k, v)


class Result:
    def __init__(self):
        self.rows = []  # (num, name, status, seconds, note)
        self.warnings = []
        self.failed = False
        self.summary_path = None
        self.outputs = {}


class JobRunner:
    def __init__(self, workflow_path, job_id, opts):
        self.path = workflow_path
        self.job_id = job_id
        self.o = opts
        self.c = Color(opts.color)
        self.wf = load_yaml(workflow_path)
        jobs = self.wf.get("jobs") or {}
        if job_id not in jobs:
            raise WorkflowError("job %r not in %s (jobs: %s)" % (job_id, workflow_path, ", ".join(jobs) or "none"))
        self.job = jobs[job_id] or {}
        if "uses" in self.job:
            raise WorkflowError("job %r calls a reusable workflow (%s); run that workflow's jobs directly"
                                % (job_id, self.job["uses"]))
        self.ws = os.path.abspath(opts.workspace)
        self.res = Result()
        self.status = Status()
        self.env_file_vars = {}
        self.path_adds = []
        self.masks = set(v for v in opts.secrets.values() if v)
        self.container = None
        self.container_bash = False
        self.shim_dir = None

    # ---- output -------------------------------------------------------------------------
    def say(self, s=""):
        for m in sorted(self.masks, key=len, reverse=True):
            s = s.replace(m, "***")
        self.o.out.write(s + "\n")
        self.o.out.flush()

    def warn(self, msg):
        if msg not in self.res.warnings:
            self.res.warnings.append(msg)

    # ---- context ------------------------------------------------------------------------
    def _event_name(self):
        if self.o.event:
            return self.o.event
        on = self.wf.get("on", self.wf.get(True))
        names = [on] if isinstance(on, str) else list(on or []) if isinstance(on, (list, dict)) else []
        for pref in ("push", "pull_request"):
            if pref in names:
                return pref
        return names[0] if names else "push"

    def build_context(self):
        o = self.o
        ws = self.ws
        sha = _git(["rev-parse", "HEAD"], ws) or "0" * 40
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], ws) or "main"
        slug = _repo_slug(_git(["remote", "get-url", "origin"], ws)) or "local/%s" % os.path.basename(ws)
        event_name = self._event_name()
        event = {}
        if o.event_file:
            with open(o.event_file, encoding="utf-8") as fh:
                event = json.load(fh)
        self.temp = tempfile.mkdtemp(prefix="jobrepro-")
        tool_cache = os.path.join(self.temp, "toolcache")
        os.makedirs(tool_cache)
        runs_on = to_str(self.job.get("runs-on"))
        if self.o.image or self.job.get("container"):
            ros = "Linux"
        else:
            ros = {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), "Linux")
        if ("windows" in runs_on.lower() and ros != "Windows") or ("macos" in runs_on.lower() and ros != "macOS"):
            self.warn("job runs on %s but this run is on %s: OS-specific steps may behave differently" % (runs_on, ros))
        arch = {"x86_64": "X64", "AMD64": "X64", "arm64": "ARM64", "aarch64": "ARM64"}.get(platform.machine(), "X64")
        github = {
            "action": "", "action_path": "", "actor": os.environ.get("USER", "jobrepro"),
            "api_url": "https://api.github.com", "base_ref": "", "event": event, "event_name": event_name,
            "event_path": os.path.join(self.temp, "event.json"), "graphql_url": "https://api.github.com/graphql",
            "head_ref": "", "job": self.job_id, "ref": "refs/heads/%s" % branch, "ref_name": branch,
            "ref_type": "branch", "repository": slug, "repository_owner": slug.split("/")[0],
            "retention_days": "90", "run_attempt": "1", "run_id": "1", "run_number": "1",
            "server_url": "https://github.com", "sha": sha, "token": o.secrets.get("GITHUB_TOKEN", ""),
            "workflow": to_str(self.wf.get("name") or os.path.relpath(self.path, ws)),
            "workflow_ref": "", "workspace": ws,
        }
        if event_name.startswith("pull_request"):
            github["head_ref"] = branch
            github["base_ref"] = (event.get("pull_request") or {}).get("base", {}).get("ref", "main")
        with open(github["event_path"], "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        inputs = {}
        on = self.wf.get("on", self.wf.get(True))
        if isinstance(on, dict):
            for trig in ("workflow_dispatch", "workflow_call"):
                spec = (on.get(trig) or {}).get("inputs") or {}
                for k, v in spec.items():
                    if isinstance(v, dict) and "default" in v:
                        inputs[k] = v["default"]
        inputs.update(o.inputs)
        ctx = {
            "github": github, "env": {}, "vars": dict(o.vars), "secrets": dict(o.secrets), "inputs": inputs,
            "matrix": {}, "strategy": {"fail-fast": True, "job-index": 0, "job-total": 1, "max-parallel": 1},
            "needs": {}, "runner": {"os": ros, "arch": arch, "name": "jobrepro", "temp": self.temp,
                                    "tool_cache": tool_cache, "debug": "1" if o.debug else ""},
            "job": {"status": "success", "container": {}, "services": {}}, "steps": {},
        }
        ctx["secrets"].setdefault("GITHUB_TOKEN", o.secrets.get("GITHUB_TOKEN", ""))
        for k, v in o.sets.items():
            _set_path(ctx, k, v)
        self.ctx = ctx
        return ctx

    def choose_matrix(self):
        strategy = self.job.get("strategy") or {}
        combos = expand_matrix(strategy.get("matrix"), self.ctx if strategy.get("matrix") is not None else None) \
            if not (isinstance(strategy.get("matrix"), str) and self.o.matrix) else [dict(self.o.matrix)]
        picked = []
        for c in combos:
            ok = True
            for k, raw in self.o.matrix.items():
                have = c.get(k)
                if to_str(have) != raw and to_str(have) != to_str(coerce_value(raw)):
                    ok = False
            if ok:
                picked.append(c)
        if not picked:
            if combos == [{}] or not self.o.matrix:
                picked = combos
            else:
                shown = "; ".join(", ".join("%s=%s" % (k, to_str(v)) for k, v in c.items()) for c in combos)
                raise WorkflowError("no matrix combination matches %s. Combinations: %s" % (
                    ", ".join("%s=%s" % kv for kv in self.o.matrix.items()), shown))
        if len(picked) > 1:
            self.warn("%d matrix combinations match; running the first. Pick one with --matrix KEY=VALUE"
                      % len(picked))
        combo = picked[0]
        self.ctx["matrix"] = combo
        self.ctx["strategy"]["job-total"] = len(combos)
        self.ctx["strategy"]["job-index"] = combos.index(combo) if combo in combos else 0
        return combo

    def _render(self, v, ctx=None):
        try:
            return render(v, ctx or self.ctx, self.status, self.ws)
        except ExprError as e:
            raise WorkflowError("expression error in %r: %s" % (v, e))

    def _render_env(self, mapping, ctx):
        out = {}
        if isinstance(mapping, str):
            mapping = self._render(mapping, ctx)
        for k, v in (mapping or {}).items():
            out[str(k)] = to_str(self._render(v, ctx))
            ctx["env"] = dict(ctx["env"], **{str(k): out[str(k)]})
        return out

    # ---- environment ----------------------------------------------------------------
    def default_env(self):
        c = self.ctx
        g = c["github"]
        e = {
            "GITHUB_WORKFLOW": g["workflow"], "GITHUB_RUN_ID": g["run_id"], "GITHUB_RUN_NUMBER": g["run_number"],
            "GITHUB_RUN_ATTEMPT": g["run_attempt"], "GITHUB_JOB": self.job_id, "GITHUB_ACTOR": g["actor"],
            "GITHUB_REPOSITORY": g["repository"], "GITHUB_REPOSITORY_OWNER": g["repository_owner"],
            "GITHUB_EVENT_NAME": g["event_name"], "GITHUB_EVENT_PATH": g["event_path"], "GITHUB_WORKSPACE": self.ws,
            "GITHUB_SHA": g["sha"], "GITHUB_REF": g["ref"], "GITHUB_REF_NAME": g["ref_name"],
            "GITHUB_REF_TYPE": g["ref_type"], "GITHUB_HEAD_REF": g["head_ref"], "GITHUB_BASE_REF": g["base_ref"],
            "GITHUB_SERVER_URL": g["server_url"], "GITHUB_API_URL": g["api_url"], "GITHUB_GRAPHQL_URL": g["graphql_url"],
            "RUNNER_OS": c["runner"]["os"], "RUNNER_ARCH": c["runner"]["arch"], "RUNNER_NAME": "jobrepro",
            "RUNNER_TEMP": self.temp, "RUNNER_TOOL_CACHE": c["runner"]["tool_cache"], "JOBREPRO": "1",
        }
        if self.o.ci_env:
            e["CI"] = "true"
            e["GITHUB_ACTIONS"] = "true"
        if self.o.debug:
            e["RUNNER_DEBUG"] = "1"
        return e

    # ---- step selection ---------------------------------------------------------------
    def plan(self, steps):
        o = self.o
        disp = [step_display_name(s, self.ctx) for s in steps]
        selected = []
        started = o.from_step is None
        stopped = False
        for i, (s, d) in enumerate(zip(steps, disp), 1):
            if not started and StepSel(o.from_step).matches(i, s, d):
                started = True
            sel = started and not stopped
            if o.only:
                sel = any(StepSel(x).matches(i, s, d) for x in o.only)
            if any(StepSel(x).matches(i, s, d) for x in o.skip):
                sel = False
            selected.append(sel)
            if o.until_step is not None and StepSel(o.until_step).matches(i, s, d):
                stopped = True
        for name, spec in (("--from", o.from_step), ("--until", o.until_step)):
            if spec is not None and not any(StepSel(spec).matches(i, s, d) for i, (s, d) in enumerate(zip(steps, disp), 1)):
                raise WorkflowError("%s %r matches no step (steps: %s)" % (name, spec, self._steps_hint(disp)))
        for spec in list(o.only) + list(o.breaks):
            if not any(StepSel(spec).matches(i, s, d) for i, (s, d) in enumerate(zip(steps, disp), 1)):
                raise WorkflowError("step %r matches no step (steps: %s)" % (spec, self._steps_hint(disp)))
        return disp, selected

    @staticmethod
    def _steps_hint(disp):
        return "; ".join("%d %s" % (i, d) for i, d in enumerate(disp, 1))

    # ---- main --------------------------------------------------------------------------
    def run(self):
        self.build_context()
        combo = self.choose_matrix()
        c = self.c
        title = job_display_name(self.job_id, self.job, combo, self.ctx)
        self.say(c("1", "jobrepro: %s / %s" % (os.path.relpath(self.path, self.ws), title)))
        wf_env = self._render_env(self.wf.get("env"), self.ctx)
        self.ctx["env"] = dict(wf_env)
        job_env = self._render_env(self.job.get("env"), self.ctx)
        self.base_env = dict(wf_env, **job_env)
        self.ctx["env"] = dict(self.base_env)
        if self.job.get("services"):
            self.warn("services (%s) are not started; run them yourself (e.g. docker run -p ...) and set the env"
                      % ", ".join(self.job["services"]))
        if self.job.get("if") is not None:
            if not eval_if(self.job["if"], self.ctx, self.status, self.ws):
                self.warn("job-level if: %r is false for this context; running anyway" % self.job["if"])
        image = self.o.image
        cont = self.job.get("container")
        if not image and cont:
            image = self._render(cont if isinstance(cont, str) else cont.get("image"))
        steps = self.job.get("steps") or []
        if not steps:
            raise WorkflowError("job %r has no steps" % self.job_id)
        disp, selected = self.plan(steps)
        if image and not self.o.dry_run:
            self.start_container(to_str(image), cont if isinstance(cont, dict) else {})
        try:
            for i, (step, name, sel) in enumerate(zip(steps, disp, selected), 1):
                if not sel:
                    self.res.rows.append((i, name, "not selected", 0, ""))
                    continue
                self.run_step(i, step, name, self.ctx, self.res.rows, "")
                if self.aborted:
                    break
        finally:
            self.stop_container()
        self.finish()
        return self.res

    aborted = False

    # ---- defaults ----------------------------------------------------------------------
    def _defaults(self):
        wd = ((self.wf.get("defaults") or {}).get("run") or {})
        jd = ((self.job.get("defaults") or {}).get("run") or {})
        return dict(wd, **jd)

    def resolve_shell(self, step, defaults, required=False):
        shell = step.get("shell") or defaults.get("shell")
        if shell is None:
            if required:
                raise WorkflowError("composite action steps must set shell:")
            has_bash = self.container_bash if self.container else bool(shutil.which("bash"))
            return ("bash -e {0}" if has_bash else "sh -e {0}"), ".sh"
        shell = to_str(self._render(shell))
        if shell in SHELLS:
            return SHELLS[shell], EXT.get(shell, ".sh")
        if "{0}" not in shell:
            raise WorkflowError("custom shell %r must contain {0}" % shell)
        return shell, ".sh"

    # ---- steps -------------------------------------------------------------------------
    def step_env(self, step, ctx):
        env = dict(self.base_env)
        env.update(self.env_file_vars)
        ctx["env"] = dict(env)
        env.update(self._render_env(step.get("env"), ctx))
        ctx["env"] = dict(env)
        return env

    def run_step(self, i, step, name, ctx, rows, indent):
        c = self.c
        label = "%s%s %s" % (indent, c("36", "[%s]" % i), name)
        sid = str(step.get("id") or "__step%s" % str(i).replace(".", "_"))
        try:
            cond = eval_if(step.get("if"), ctx, self.status, self.ws)
        except ExprError as e:
            raise WorkflowError("step %s if: %s" % (i, e))
        if not cond:
            self.say("%s %s" % (label, c("2", "skipped (if: false)")))
            ctx["steps"][sid] = {"outputs": {}, "outcome": "skipped", "conclusion": "skipped"}
            rows.append((i, name, "skipped (if)", 0, ""))
            return
        env = self.step_env(step, ctx)
        if "uses" in step:
            self.run_uses(i, step, name, ctx, env, rows, indent, sid, label)
            return
        if "run" not in step:
            raise WorkflowError("step %s has neither run: nor uses:" % i)
        defaults = self._defaults() if not indent else {}
        shell_tpl, ext = self.resolve_shell(step, defaults, required=bool(indent))
        script = to_str(self._render(to_str(step["run"]), ctx))
        wd = step.get("working-directory") or defaults.get("working-directory")
        cwd = os.path.join(self.ws, to_str(self._render(wd, ctx))) if wd else ctx.get("_cwd", self.ws)
        self.say(label)
        if self.o.dry_run:
            self.print_dry(script, shell_tpl, cwd, env, step)
            rows.append((i, name, "planned", 0, ""))
            return
        files = self.command_files(sid)
        full_env = self.full_env(env, files)
        path = os.path.join(self.temp, "step-%s%s" % (str(i).replace(".", "-"), ext))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script if script.endswith("\n") else script + "\n")
        argv = self.shell_argv(shell_tpl, path)
        if any(StepSel(b).matches(i if isinstance(i, int) else -1, step, name) for b in self.o.breaks) and not indent:
            self.say(c("33", "  breakpoint before step %s: shell opened with this step's env and cwd." % i))
            self.say(c("33", "  The step script is $JOBREPRO_STEP_SCRIPT. Exit 0 to run the step, exit 1 to stop."))
            if self.debug_shell(full_env, cwd, path) != 0:
                self.say(c("31", "  stopped at breakpoint"))
                rows.append((i, name, "stopped at breakpoint", 0, ""))
                self.aborted = True
                return
        timeout = step.get("timeout-minutes")
        timeout = float(to_str(self._render(timeout, ctx))) * 60 if timeout is not None else None
        forced = False
        while True:
            t0 = time.time()
            code = self.exec_streamed(argv, full_env, cwd, timeout, indent)
            dt = time.time() - t0
            if code == 0 or not self.o.break_on_failure or indent:
                break
            self.say(c("31", "  step %s failed (exit %s). Opening a shell in its env and cwd." % (i, code)))
            self.say(c("33", "  Fix things, exit the shell, then answer: [r]etry step, [c]ontinue, [a]bort"))
            self.debug_shell(full_env, cwd, path)
            ans = self.ask("  retry/continue/abort? [r/c/A] ")
            if ans.startswith("r"):
                self.reset_command_files(files)
                continue
            if ans.startswith("c"):
                forced = True
            if not ans.startswith("c"):
                self.aborted = True
            break
        self.after_step(i, name, step, sid, ctx, files, code, dt, rows, forced)

    def after_step(self, i, name, step, sid, ctx, files, code, dt, rows, forced=False):
        c = self.c
        outputs = parse_command_file(files["GITHUB_OUTPUT"])
        new_env = parse_command_file(files["GITHUB_ENV"])
        self.env_file_vars.update({k: v for k, v in new_env.items() if not k.startswith(("GITHUB_", "RUNNER_"))})
        if os.path.exists(files["GITHUB_PATH"]):
            with open(files["GITHUB_PATH"], encoding="utf-8") as fh:
                for ln in fh.read().splitlines():
                    if ln.strip() and ln.strip() not in self.path_adds:
                        self.path_adds.insert(0, ln.strip())
        coe = step.get("continue-on-error")
        coe = to_str(self._render(coe, ctx)) in ("true", "True") if isinstance(coe, str) else bool(coe)
        if forced and code != 0:
            self.say(c("33", "  failed with exit %s, continuing as you asked" % code))
            ctx["steps"][sid] = {"outputs": parse_command_file(files["GITHUB_OUTPUT"]), "outcome": "failure",
                                 "conclusion": "success"}
            rows.append((i, name, "failed (continued)", dt, "exit %s" % code))
            return
        outcome = "success" if code == 0 else "failure"
        conclusion = "success" if code == 0 or coe else "failure"
        ctx["steps"][sid] = {"outputs": outputs, "outcome": outcome, "conclusion": conclusion}
        if code == 0:
            self.say(c("32", "  ok") + c("2", " (%.1fs)" % dt))
            rows.append((i, name, "ok", dt, ""))
        elif coe:
            self.say(c("33", "  failed with exit %s, continue-on-error is set" % code))
            rows.append((i, name, "failed (continue-on-error)", dt, "exit %s" % code))
        else:
            self.say(c("31", "  failed with exit %s" % code) + c("2", " (%.1fs)" % dt))
            rows.append((i, name, "FAILED", dt, "exit %s" % code))
            self.status.failed = True
            ctx["job"]["status"] = "failure"
            self.res.failed = True

    def ask(self, prompt):
        self.o.out.write(prompt)
        self.o.out.flush()
        line = self.o.inp.readline()
        if not (hasattr(self.o.inp, "isatty") and self.o.inp.isatty()):
            self.o.out.write(line.strip() + "\n" if line else "(no answer: abort)\n")
        return line.strip().lower()

    # ---- uses: ----------------------------------------------------------------------------
    def run_uses(self, i, step, name, ctx, env, rows, indent, sid, label):
        c = self.c
        uses = to_str(step["uses"])
        bare = uses.split("@")[0]
        with_ = {str(k): to_str(self._render(v, ctx)) for k, v in (step.get("with") or {}).items()}
        ctx["steps"][sid] = {"outputs": {}, "outcome": "success", "conclusion": "success"}
        if uses.startswith("./"):
            self.run_composite(i, step, name, ctx, env, rows, indent, sid, label, uses, with_)
            return
        if bare in NOOP_ACTIONS and NOOP_ACTIONS[bare]:
            self.say("%s %s" % (label, c("2", "skipped: %s" % NOOP_ACTIONS[bare])))
            rows.append((i, name, "skipped (local no-op)", 0, ""))
            return
        if bare.startswith("actions/setup-") or bare in ("astral-sh/setup-uv", "pnpm/action-setup", "dtolnay/rust-toolchain"):
            note = self.check_setup(bare, with_)
            self.say("%s %s" % (label, c("2", note)))
            rows.append((i, name, "toolchain check", 0, note))
            return
        self.say("%s %s" % (label, c("33", "skipped: remote action %s is not run locally" % uses)))
        self.warn("step %s uses %s, which jobrepro does not run; later steps that need its effects or "
                  "outputs may fail" % (i, uses))
        rows.append((i, name, "skipped (uses)", 0, uses))

    def check_setup(self, bare, with_):
        if self.container:
            return "skipped: toolchain comes from the container image"
        if bare == "actions/setup-python":
            want = with_.get("python-version", "")
            if not want:
                f = with_.get("python-version-file")
                if f:
                    return "python-version-file %s: using local %s" % (f, self._version(["python3", "--version"]) or "python")
                return "skipped (no python-version)"
            want = want.splitlines()[0].strip()
            exe = shutil.which("python%s" % want) or shutil.which("python")
            have = self._version([exe or "python3", "--version"])
            if have and (have == want or have.startswith(want + ".")):
                self._shim({"python": exe, "python3": exe})
                return "ok: python %s found locally (%s)" % (have, exe)
            alt = shutil.which("python%s" % ".".join(want.split(".")[:2]))
            if alt:
                self._shim({"python": alt, "python3": alt})
                return "ok: using %s for python-version %s" % (alt, want)
            self.warn("CI uses Python %s but this machine has %s; results may differ" % (want, have or "none"))
            return "mismatch: CI wants python %s, local is %s" % (want, have or "missing")
        if bare == "actions/setup-node":
            want = with_.get("node-version", "")
            have = self._version(["node", "--version"]).lstrip("v")
            if want and have and not have.startswith(want.lstrip("v").split(".")[0].replace(".x", "")):
                self.warn("CI uses Node %s but this machine has %s" % (want, have))
                return "mismatch: CI wants node %s, local is %s" % (want, have)
            return "ok: node %s found locally" % have if have else "node not found locally"
        if bare == "actions/setup-go":
            want = with_.get("go-version", "")
            have = self._version(["go", "version"])
            m = re.search(r"go(\d+\.\d+(?:\.\d+)?)", have)
            have = m.group(1) if m else have
            if want and have and not have.startswith(want.replace(".x", "")):
                self.warn("CI uses Go %s but this machine has %s" % (want, have))
                return "mismatch: CI wants go %s, local is %s" % (want, have)
            return "ok: go %s found locally" % have if have else "go not found locally"
        return "skipped: %s (using whatever is installed locally)" % bare

    def _version(self, argv):
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", p.stdout)
        return m.group(1) if m else ""

    def _shim(self, mapping):
        if not self.shim_dir:
            self.shim_dir = os.path.join(self.temp, "shims")
            os.makedirs(self.shim_dir, exist_ok=True)
            self.path_adds.insert(0, self.shim_dir)
        for name, target in mapping.items():
            if target:
                link = os.path.join(self.shim_dir, name)
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(target, link)

    def run_composite(self, i, step, name, ctx, env, rows, indent, sid, label, uses, with_):
        c = self.c
        adir = os.path.join(self.ws, uses[2:])
        meta = None
        for fn in ("action.yml", "action.yaml"):
            if os.path.isfile(os.path.join(adir, fn)):
                meta = load_yaml(os.path.join(adir, fn))
        if meta is None:
            raise WorkflowError("step %s: %s has no action.yml" % (i, uses))
        runs = meta.get("runs") or {}
        if runs.get("using") != "composite":
            self.say("%s %s" % (label, c("33", "skipped: local %s action (%s) is not run" % (runs.get("using"), uses))))
            self.warn("step %s uses a local %s action; only composite actions run" % (i, runs.get("using")))
            rows.append((i, name, "skipped (uses)", 0, uses))
            return
        inputs = {}
        for k, spec in (meta.get("inputs") or {}).items():
            if isinstance(spec, dict) and "default" in spec:
                inputs[k] = to_str(self._render(spec["default"], ctx))
        inputs.update(with_)
        self.say("%s %s" % (label, c("2", "composite action %s" % uses)))
        rows.append((i, name, "composite", 0, ""))
        sub = dict(ctx)
        sub["inputs"] = inputs
        sub["steps"] = {}
        sub["env"] = dict(ctx["env"])
        sub["github"] = dict(ctx["github"], action_path=adir)
        saved_base = self.base_env
        self.base_env = dict(env, GITHUB_ACTION_PATH=adir)
        for j, s in enumerate(runs.get("steps") or [], 1):
            self.run_step("%s.%s" % (i, j), s, step_display_name(s, sub), sub, rows, indent + "  ")
            if self.status.failed:
                break
        self.base_env = saved_base
        outs = {}
        for k, spec in (meta.get("outputs") or {}).items():
            if isinstance(spec, dict) and "value" in spec:
                outs[k] = to_str(self._render(spec["value"], sub))
        failed = self.status.failed
        ctx["steps"][sid] = {"outputs": outs, "outcome": "failure" if failed else "success",
                             "conclusion": "failure" if failed else "success"}

    # ---- process plumbing ------------------------------------------------------------------
    def command_files(self, sid):
        files = {}
        tag = re.sub(r"[^A-Za-z0-9_.-]", "_", sid) + "-" + uuid.uuid4().hex[:6]
        for k in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE"):
            p = os.path.join(self.temp, "%s-%s" % (k.lower(), tag))
            open(p, "w").close()
            files[k] = p
        summ = os.path.join(self.temp, "step_summary.md")
        if not os.path.exists(summ):
            open(summ, "w").close()
        files["GITHUB_STEP_SUMMARY"] = summ
        self.res.summary_path = summ
        return files

    @staticmethod
    def reset_command_files(files):
        for k, p in files.items():
            if k != "GITHUB_STEP_SUMMARY":
                open(p, "w").close()

    def full_env(self, env, files):
        if self.container:
            base = {}
        else:
            base = dict(os.environ)
        base.update(self.default_env())
        base.update(env)
        base.update(files)
        if self.path_adds:
            sep = os.pathsep
            base["PATH"] = sep.join(self.path_adds + [base.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")])
        return base

    @staticmethod
    def shell_argv(tpl, path):
        parts = shlex.split(tpl)
        return [p.replace("{0}", path) for p in parts]

    def exec_argv(self, argv, env, cwd, interactive=False):
        if not self.container:
            return argv
        flags = ["-i"] + (["-t"] if interactive and sys.stdin.isatty() else [])
        envflags = []
        for k, v in env.items():
            envflags += ["-e", "%s=%s" % (k, v)]
        return ["docker", "exec"] + flags + ["-w", cwd] + envflags + [self.container] + argv

    def exec_streamed(self, argv, env, cwd, timeout, indent):
        c = self.c
        full = self.exec_argv(argv, env, cwd)
        try:
            p = subprocess.Popen(full, cwd=cwd if not self.container else None, env=env if not self.container else None,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            self.say(c("31", "  cannot start %s: %s" % (full[0], e.strerror)))
            return 127
        except NotADirectoryError:
            self.say(c("31", "  working directory %s does not exist" % cwd))
            return 1
        timed_out = []
        timer = None
        if timeout:
            def _kill():
                timed_out.append(True)
                try:
                    p.kill()
                except OSError:
                    pass
            timer = threading.Timer(timeout, _kill)
            timer.daemon = True
            timer.start()
        stop_token = None
        pad = indent + "  "
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
            if stop_token is None and line.startswith("::"):
                m = re.match(r"^::([a-zA-Z-]+)(?: ([^:]*))?::(.*)$", line)
                if m:
                    cmd, props, msg = m.group(1), m.group(2) or "", m.group(3)
                    if cmd == "add-mask":
                        if msg:
                            self.masks.add(msg)
                        continue
                    if cmd == "stop-commands":
                        stop_token = msg
                        continue
                    if cmd == "group":
                        self.say(pad + c("1", "▸ " + msg))
                        continue
                    if cmd == "endgroup":
                        continue
                    if cmd in ("error", "warning", "notice"):
                        col = {"error": "31", "warning": "33", "notice": "36"}[cmd]
                        where = (" (%s)" % props) if props else ""
                        self.say(pad + c(col, "%s%s: %s" % (cmd, where, msg)))
                        continue
                    if cmd == "debug":
                        if self.o.debug:
                            self.say(pad + c("2", "debug: " + msg))
                        continue
                    if cmd in ("set-output", "save-state", "set-env"):
                        self.say(pad + c("33", "(deprecated ::%s:: command ignored, as on GitHub)" % cmd))
                        continue
            elif stop_token is not None and line == "::%s::" % stop_token:
                stop_token = None
                continue
            self.say(pad + line)
        code = p.wait()
        if timer:
            timer.cancel()
        if timed_out:
            self.say(c("31", "  timeout-minutes exceeded"))
            return 124
        return code

    def debug_shell(self, env, cwd, script_path):
        env = dict(env, JOBREPRO_STEP_SCRIPT=script_path, PS1="[jobrepro] \\w $ ")
        custom = os.environ.get("JOBREPRO_SHELL")
        if custom:
            argv = shlex.split(custom)
        elif self.container:
            argv = ["bash", "--norc", "-i"] if self.container_bash else ["sh", "-i"]
        else:
            sh = shutil.which("bash")
            argv = [sh, "--norc", "-i"] if sh else [os.environ.get("SHELL", "/bin/sh"), "-i"]
        full = self.exec_argv(argv, env, cwd, interactive=True)
        try:
            return subprocess.call(full, cwd=cwd if not self.container else None,
                                   env=env if not self.container else None)
        except OSError as e:
            self.say("  cannot open shell: %s" % e)
            return 1

    # ---- containers ---------------------------------------------------------------------
    def start_container(self, image, spec):
        if not shutil.which("docker"):
            raise WorkflowError("this job needs a container (%s) but docker is not on PATH; drop --image or install "
                                "docker" % image)
        name = "jobrepro-%s" % uuid.uuid4().hex[:8]
        argv = ["docker", "run", "-d", "--rm", "--name", name, "-v", "%s:%s" % (self.ws, self.ws),
                "-v", "%s:%s" % (self.temp, self.temp), "-w", self.ws, "--entrypoint", "sh"]
        for k, v in (spec.get("env") or {}).items():
            argv += ["-e", "%s=%s" % (k, to_str(self._render(v)))]
        opts = spec.get("options")
        if opts:
            argv += shlex.split(to_str(opts))
        argv += [image, "-c", "while sleep 3600; do :; done"]
        self.say(self.c("2", "  starting container %s from %s" % (name, image)))
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode:
            raise WorkflowError("docker run failed: %s" % p.stdout.strip())
        self.container = name
        chk = subprocess.run(["docker", "exec", name, "sh", "-c", "command -v bash"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        self.container_bash = chk.returncode == 0

    def stop_container(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container = None

    # ---- reporting ----------------------------------------------------------------------
    def print_dry(self, script, shell_tpl, cwd, env, step):
        c = self.c
        self.say(c("2", "  shell: %s   cwd: %s" % (shell_tpl, os.path.relpath(cwd, self.ws))))
        keys = [k for k in env]
        if keys:
            self.say(c("2", "  env: %s" % ", ".join("%s=%s" % (k, env[k]) for k in keys)))
        for ln in script.rstrip("\n").splitlines():
            self.say("  │ " + ln)

    def finish(self):
        c = self.c
        self.say("")
        self.say(c("1", "summary"))
        hidden = sum(1 for r in self.res.rows if r[2] == "not selected")
        for num, name, status, dt, note in self.res.rows:
            if status == "not selected":
                continue
            col = "32" if status == "ok" else "31" if status in ("FAILED", "stopped at breakpoint") else "2"
            if status.startswith("skipped (uses") or status.startswith("failed ("):
                col = "33"
            extra = (" %.1fs" % dt) if dt else ""
            self.say("  %-5s %-44s %s%s%s" % (num, name[:44], c(col, status), extra, ("  " + note) if note else ""))
        if hidden:
            self.say(c("2", "  (%d step%s not selected)" % (hidden, "" if hidden == 1 else "s")))
        if self.res.summary_path and os.path.getsize(self.res.summary_path):
            self.say(c("2", "  step summary written to %s" % self.res.summary_path))
        missing = sorted(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", json.dumps(self.job))) -
                         set(k for k, v in self.o.secrets.items() if v))
        if missing:
            self.warn("secrets not provided (empty, as on a fork PR): %s. Use --secret NAME=VALUE or "
                      "--secrets-file .secrets" % ", ".join(missing))
        for w in self.res.warnings:
            self.say(c("33", "  warning: " + w))
        if self.o.dry_run:
            self.say(c("2", "dry run: nothing was executed (step outputs from earlier steps show as empty)"))
        elif self.aborted and not self.res.failed:
            self.say(c("31", "stopped"))
        else:
            self.say(c("31", "job failed") if self.res.failed else c("32", "job succeeded"))
