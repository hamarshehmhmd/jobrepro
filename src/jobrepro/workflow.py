# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load workflow files, expand matrices, and compute GitHub's display names."""
import itertools
import os
import re

import yaml

from .expr import ExprError, render, to_str


class WorkflowError(Exception):
    pass


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps `on:` as the string "on" (YAML 1.1 would make it True)."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_Loader)
    except OSError as e:
        raise WorkflowError("cannot read %s: %s" % (path, e.strerror))
    except yaml.YAMLError as e:
        raise WorkflowError("%s is not valid YAML: %s" % (path, e))
    if not isinstance(data, dict):
        raise WorkflowError("%s does not look like a workflow (top level is not a mapping)" % path)
    return data


def load_yaml_text(text, name="<workflow>"):
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        raise WorkflowError("%s is not valid YAML: %s" % (name, e))
    if not isinstance(data, dict):
        raise WorkflowError("%s does not look like a workflow" % name)
    return data


def find_workflows(root="."):
    d = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.join(d, f) for f in os.listdir(d) if f.endswith((".yml", ".yaml"))
    )


def resolve_target(target, root="."):
    """`ci.yml:test`, `.github/workflows/ci.yml:test`, `test` (searched) -> (path, job_id)."""
    if ":" in target:
        wf, job = target.rsplit(":", 1)
        cands = [wf, os.path.join(root, wf), os.path.join(root, ".github", "workflows", wf)]
        for c in cands:
            if os.path.isfile(c):
                return c, job
        raise WorkflowError("workflow file %r not found (looked in . and .github/workflows)" % wf)
    hits = []
    for path in find_workflows(root):
        jobs = load_yaml(path).get("jobs") or {}
        if target in jobs:
            hits.append(path)
    if not hits:
        raise WorkflowError("no workflow in .github/workflows has a job %r (try `jobrepro list`)" % target)
    if len(hits) > 1:
        raise WorkflowError(
            "job %r is in several workflows: %s; use FILE:JOB" % (target, ", ".join(os.path.basename(h) for h in hits))
        )
    return hits[0], target


def _plain(v):
    if isinstance(v, float) and v.is_integer():
        return v
    return v


def expand_matrix(matrix, ctx=None):
    """Return the list of matrix combinations (dicts), following include/exclude rules."""
    if matrix is None:
        return [{}]
    if isinstance(matrix, str):
        if ctx is None:
            raise WorkflowError("matrix is an expression (%s); pass values with --matrix KEY=VALUE" % matrix)
        try:
            matrix = render(matrix, ctx)
        except ExprError as e:
            raise WorkflowError("cannot evaluate matrix: %s" % e)
        if not isinstance(matrix, dict):
            raise WorkflowError("matrix expression did not produce a mapping")
    base = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    for k, v in base.items():
        if isinstance(v, str) and "${{" in v:
            if ctx is None:
                raise WorkflowError("matrix.%s is an expression; pass --matrix %s=VALUE" % (k, k))
            v = render(v, ctx)
            base[k] = v
        if not isinstance(base[k], list):
            raise WorkflowError("matrix.%s must be a list" % k)
    keys = list(base)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(base[k] for k in keys))] if keys else []
    for ex in matrix.get("exclude") or []:
        combos = [c for c in combos if not all(_match(c.get(k), v) for k, v in ex.items())]
    original_keys = set(keys)
    originals = list(combos)
    for inc in matrix.get("include") or []:
        matched = False
        for c in originals:
            # An include entry extends every original combination whose original values it does not
            # overwrite; values added by earlier include entries may be overwritten.
            if all(_match(c.get(k), v) for k, v in inc.items() if k in original_keys):
                for k, v in inc.items():
                    if k not in original_keys:
                        c[k] = v
                matched = True
        if not matched:
            combos.append(dict(inc))
    return combos or [{}]


def _match(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return all(_match(a.get(k), v) for k, v in b.items())
    return to_str(a) == to_str(b)


def matrix_label(combo):
    """GitHub's `(v1, v2)` suffix; nested mappings are shown as their values."""
    parts = []
    for v in combo.values():
        if isinstance(v, dict):
            parts.append(", ".join(to_str(x) for x in v.values()))
        else:
            parts.append(to_str(v))
    return ", ".join(parts)


def job_display_name(job_id, job, combo, ctx):
    name = job.get("name")
    if name is None:
        return job_id + (" (%s)" % matrix_label(combo) if combo else "")
    name = str(name)
    try:
        shown = to_str(render(name, ctx))
    except ExprError:
        shown = name
    if combo and "matrix." not in name.replace(" ", ""):
        shown += " (%s)" % matrix_label(combo)
    return shown


def step_display_name(step, ctx=None):
    if step.get("name") is not None:
        n = str(step["name"])
        if ctx is not None and "${{" in n:
            try:
                return to_str(render(n, ctx))
            except ExprError:
                return n
        return n
    if step.get("uses"):
        return "Run %s" % step["uses"]
    run = str(step.get("run") or "")
    first = next((ln.strip() for ln in run.splitlines() if ln.strip()), "")
    return "Run %s" % first


def coerce_value(text):
    """Turn a CLI `--matrix k=v` value into the type YAML would give it."""
    try:
        v = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError:
        return text
    if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
        return v if v is not None else text
    return text
