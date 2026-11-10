# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small evaluator for GitHub Actions ``${{ }}`` expressions.

Covers literals, context access (``a.b``, ``a['b']``, ``a.*.b``), the operators
``! == != < <= > >= && ||`` with GitHub's loose comparison rules, and the
documented functions (contains, startsWith, endsWith, format, join, toJSON,
fromJSON, hashFiles, success, failure, always, cancelled).
"""
import glob
import hashlib
import json
import math
import os
import re

__all__ = ["ExprError", "evaluate", "render", "to_str", "truthy", "Status"]


class ExprError(ValueError):
    pass


class Status:
    """Job status seen by success()/failure()/always()/cancelled()."""

    def __init__(self):
        self.failed = False
        self.cancelled = False


_TOKEN = re.compile(
    r"\s*(?:(?P<num>0x[0-9a-fA-F]+|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
    r"|(?P<str>'(?:[^']|'')*')"
    r"|(?P<op>==|!=|<=|>=|&&|\|\||[!<>()\[\],.*])"
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_-]*))"
)


def _tokenize(src):
    pos, out = 0, []
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ExprError("cannot parse expression near %r" % src[pos:pos + 20])
        pos = m.end()
        for kind in ("num", "str", "op", "id"):
            v = m.group(kind)
            if v is not None:
                out.append((kind, v))
                break
    return out


class _Wildcard(list):
    """Result of a ``.*`` filter: a list that keeps filtering on property access."""


def to_str(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, indent=2)
    return str(v)


def to_num(v):
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
        except ValueError:
            return math.nan
    return math.nan


def truthy(v):
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not math.isnan(v)
    if isinstance(v, str):
        return v != ""
    return True


def _loose_eq(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    if a is None and b is None:
        return True
    na, nb = to_num(a), to_num(b)
    return na == nb


def _cmp(a, b, op):
    if isinstance(a, str) and isinstance(b, str):
        x, y = a.lower(), b.lower()
    else:
        x, y = to_num(a), to_num(b)
        if math.isnan(x) or math.isnan(y):
            return False
    return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]


def _index(obj, key):
    if isinstance(obj, _Wildcard):
        out = _Wildcard()
        for item in obj:
            v = _index(item, key)
            if v is not None:
                out.append(v)
        return out
    if isinstance(obj, dict):
        if isinstance(key, str):
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() == key.lower():
                    return v
            return None
        return obj.get(key)
    if isinstance(obj, list):
        n = to_num(key)
        if math.isnan(n) or n < 0 or int(n) >= len(obj):
            return None
        return obj[int(n)]
    return None


def _fmt(fmt, *args):
    def rep(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        i = int(m.group(1))
        if i >= len(args):
            raise ExprError("format(): missing argument {%d}" % i)
        return to_str(args[i])
    return re.sub(r"\{\{|\}\}|\{(\d+)\}", rep, to_str(fmt))


def hash_files(workspace, patterns):
    """hashFiles(): sha256 over the sha256 of each matched file, sorted by path."""
    files = set()
    excludes = set()
    for pat in patterns:
        pat = to_str(pat)
        neg = pat.startswith("!")
        if neg:
            pat = pat[1:]
        for p in glob.glob(os.path.join(workspace, pat), recursive=True):
            if os.path.isfile(p):
                (excludes if neg else files).add(os.path.abspath(p))
    files -= excludes
    if not files:
        return ""
    outer = hashlib.sha256()
    for p in sorted(files):
        with open(p, "rb") as fh:
            outer.update(hashlib.sha256(fh.read()).digest())
    return outer.hexdigest()


class _Parser:
    def __init__(self, tokens, ctx, status, workspace):
        self.t = tokens
        self.i = 0
        self.ctx = ctx
        self.status = status
        self.workspace = workspace

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def eat(self, value=None):
        tok = self.peek()
        if tok[0] is None or (value is not None and tok[1] != value):
            raise ExprError("expected %r, got %r" % (value, tok[1]))
        self.i += 1
        return tok

    def parse(self):
        v = self.or_()
        if self.i != len(self.t):
            raise ExprError("unexpected %r" % (self.peek()[1],))
        return v

    def or_(self):
        v = self.and_()
        while self.peek()[1] == "||":
            self.eat()
            rhs = self.and_()
            v = v if truthy(v) else rhs
        return v

    def and_(self):
        v = self.eq()
        while self.peek()[1] == "&&":
            self.eat()
            rhs = self.eq()
            v = rhs if truthy(v) else v
        return v

    def eq(self):
        v = self.rel()
        while self.peek()[1] in ("==", "!="):
            op = self.eat()[1]
            rhs = self.rel()
            r = _loose_eq(v, rhs)
            v = r if op == "==" else not r
        return v

    def rel(self):
        v = self.unary()
        while self.peek()[1] in ("<", "<=", ">", ">="):
            op = self.eat()[1]
            v = _cmp(v, self.unary(), op)
        return v

    def unary(self):
        if self.peek()[1] == "!":
            self.eat()
            return not truthy(self.unary())
        return self.postfix()

    def postfix(self):
        v = self.primary()
        while True:
            tok = self.peek()
            if tok[1] == ".":
                self.eat()
                nxt = self.eat()
                if nxt[1] == "*":
                    v = _Wildcard(v.values() if isinstance(v, dict) else (v if isinstance(v, list) else []))
                else:
                    v = _index(v, nxt[1])
            elif tok[1] == "[":
                self.eat()
                if self.peek()[1] == "*":
                    self.eat()
                    v = _Wildcard(v.values() if isinstance(v, dict) else (v if isinstance(v, list) else []))
                else:
                    v = _index(v, self.or_())
                self.eat("]")
            else:
                return v

    def primary(self):
        kind, val = self.eat()
        if kind == "num":
            return float(int(val, 16)) if val.lower().startswith("0x") else float(val)
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if val == "(":
            v = self.or_()
            self.eat(")")
            return v
        if kind != "id":
            raise ExprError("unexpected %r" % val)
        low = val.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low == "null":
            return None
        if self.peek()[1] == "(":
            self.eat("(")
            args = []
            while self.peek()[1] != ")":
                args.append(self.or_())
                if self.peek()[1] == ",":
                    self.eat(",")
            self.eat(")")
            return self.call(low, args)
        return _index(self.ctx, val)

    def call(self, name, args):
        st = self.status
        if name == "success":
            return not st.failed and not st.cancelled
        if name == "failure":
            return st.failed
        if name == "always":
            return True
        if name == "cancelled":
            return st.cancelled
        if name == "contains":
            hay, needle = args[0], args[1]
            if isinstance(hay, list):
                return any(_loose_eq(x, needle) for x in hay)
            return to_str(needle).lower() in to_str(hay).lower()
        if name == "startswith":
            return to_str(args[0]).lower().startswith(to_str(args[1]).lower())
        if name == "endswith":
            return to_str(args[0]).lower().endswith(to_str(args[1]).lower())
        if name == "format":
            return _fmt(*args)
        if name == "join":
            sep = to_str(args[1]) if len(args) > 1 else ","
            if isinstance(args[0], list):
                return sep.join(to_str(x) for x in args[0])
            return to_str(args[0])
        if name == "tojson":
            return json.dumps(args[0], indent=2)
        if name == "fromjson":
            try:
                v = json.loads(to_str(args[0]))
            except ValueError as e:
                raise ExprError("fromJSON(): %s" % e)
            return float(v) if isinstance(v, int) and not isinstance(v, bool) else v
        if name == "hashfiles":
            return hash_files(self.workspace, args)
        raise ExprError("unknown function %s()" % name)


def evaluate(src, ctx, status=None, workspace="."):
    return _Parser(_tokenize(src), ctx, status or Status(), workspace).parse()


_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def render(text, ctx, status=None, workspace="."):
    """Substitute every ``${{ expr }}`` in *text*. A lone expression keeps its type."""
    if not isinstance(text, str):
        return text
    m = _EXPR.fullmatch(text.strip())
    if m and text.strip() == text:
        return evaluate(m.group(1), ctx, status, workspace)
    return _EXPR.sub(lambda m: to_str(evaluate(m.group(1), ctx, status, workspace)), text)


def eval_if(cond, ctx, status, workspace="."):
    """Evaluate a step/job ``if:``. Implicit success() unless a status function is used."""
    if cond is None or cond == "":
        return not status.failed and not status.cancelled
    if isinstance(cond, bool):
        return cond and not status.failed
    s = str(cond).strip()
    m = _EXPR.fullmatch(s)
    if m:
        s = m.group(1).strip()
    elif "${{" in s:
        s = to_str(render(s, ctx, status, workspace))
        return truthy(s) and not status.failed
    if not re.search(r"\b(success|failure|always|cancelled)\s*\(", s):
        s = "success() && (%s)" % s
    return truthy(evaluate(s, ctx, status, workspace))
