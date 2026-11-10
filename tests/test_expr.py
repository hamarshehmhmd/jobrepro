import json

import pytest

from jobrepro.expr import ExprError, Status, eval_if, evaluate, hash_files, render, to_str, truthy

CTX = {
    "github": {"ref": "refs/heads/main", "event_name": "push", "event": {"commits": [{"id": "a"}, {"id": "b"}]}},
    "matrix": {"python": "3.12", "os": "ubuntu-latest", "n": 2},
    "env": {"FOO": "bar"},
    "steps": {"meta": {"outputs": {"tag": "v1"}, "outcome": "success"}},
    "labels": ["bug", "Help Wanted"],
}


def ev(s):
    return evaluate(s, CTX)


def test_literals_and_access():
    assert ev("'it''s'") == "it's"
    assert ev("42") == 42 and ev("0xff") == 255 and ev("-1.5") == -1.5
    assert ev("true") is True and ev("null") is None
    assert ev("github.ref") == "refs/heads/main"
    assert ev("github['event_name']") == "push"
    assert ev("matrix.PYTHON") == "3.12"  # case-insensitive keys
    assert ev("steps.meta.outputs.tag") == "v1"
    assert ev("steps.missing.outputs.tag") is None
    assert ev("github.event.commits.*.id") == ["a", "b"]


def test_operators_and_loose_equality():
    assert ev("github.ref == 'REFS/HEADS/MAIN'") is True  # strings compare case-insensitively
    assert ev("matrix.n == '2'") is True  # number vs string coerces
    assert ev("matrix.n > 1 && matrix.n < 3") is True
    assert ev("!env.FOO") is False
    assert ev("env.NOPE || 'fallback'") == "fallback"
    assert ev("env.FOO && 'yes'") == "yes"
    assert ev("null == 0") is True
    assert ev("(1 == 2) || (2 == 2)") is True


def test_functions():
    assert ev("contains(labels, 'help wanted')") is True
    assert ev("contains('Hello world', 'WORLD')") is True
    assert ev("startsWith(github.ref, 'refs/heads/')") and ev("endsWith(github.ref, 'main')")
    assert ev("format('{0}-{1}-{{x}}', matrix.python, matrix.os)") == "3.12-ubuntu-latest-{x}"
    assert ev("join(labels, ', ')") == "bug, Help Wanted"
    assert json.loads(ev("toJSON(matrix)"))["python"] == "3.12"
    assert ev("fromJSON('[1,2]')") == [1.0, 2.0]
    assert ev("fromJSON('{\"a\": true}').a") is True
    with pytest.raises(ExprError):
        ev("nosuch(1)")
    with pytest.raises(ExprError):
        ev("github.ref ==")


def test_status_functions():
    st = Status()
    assert evaluate("success()", CTX, st) and not evaluate("failure()", CTX, st)
    st.failed = True
    assert evaluate("failure()", CTX, st) and evaluate("always()", CTX, st)


def test_render_keeps_type_for_lone_expression():
    assert render("${{ matrix.n }}", CTX) == 2
    assert render("py${{ matrix.python }}-${{ env.FOO }}", CTX) == "py3.12-bar"
    assert render("plain", CTX) == "plain"
    assert to_str(True) == "true" and to_str(3.0) == "3" and to_str(None) == ""


def test_eval_if_implicit_success():
    st = Status()
    assert eval_if(None, CTX, st)
    assert eval_if("github.event_name == 'push'", CTX, st)
    assert eval_if("${{ github.event_name == 'pull_request' }}", CTX, st) is False
    st.failed = True
    assert not eval_if("github.event_name == 'push'", CTX, st)  # implicit success() && ...
    assert eval_if("failure()", CTX, st)
    assert eval_if("always() && matrix.n == 2", CTX, st)


def test_truthy():
    assert not truthy("") and not truthy(0) and not truthy(None) and truthy("0") and truthy([])


def test_hash_files(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    h1 = hash_files(str(tmp_path), ["**/*.txt"])
    assert len(h1) == 64
    assert hash_files(str(tmp_path), ["**/*.txt", "!sub/**"]) != h1
    assert hash_files(str(tmp_path), ["*.nope"]) == ""
    assert evaluate("hashFiles('**/*.txt')", {}, workspace=str(tmp_path)) == h1
