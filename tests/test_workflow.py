import pytest

from jobrepro.runner import parse_command_file, parse_dotenv
from jobrepro.workflow import (
    WorkflowError,
    expand_matrix,
    job_display_name,
    load_yaml_text,
    resolve_target,
    step_display_name,
)


def test_on_key_stays_a_string():
    wf = load_yaml_text("on: [push]\njobs: {}\n")
    assert "on" in wf and True not in wf


def test_matrix_product_exclude_include():
    m = {"os": ["linux", "mac"], "py": ["3.12", "3.13"],
         "exclude": [{"os": "mac", "py": "3.12"}],
         "include": [{"py": "3.13", "extra": "lint"}, {"os": "windows", "py": "3.13"}]}
    combos = expand_matrix(m)
    assert {"os": "mac", "py": "3.12"} not in combos
    assert {"os": "linux", "py": "3.13", "extra": "lint"} in combos
    assert {"os": "mac", "py": "3.13", "extra": "lint"} in combos
    assert {"os": "windows", "py": "3.13"} in combos
    assert len(combos) == 4


def test_include_only_matrix():
    combos = expand_matrix({"include": [{"python": "3.14"}, {"name": "Mac", "python": "3.14", "os": "macos"}]})
    assert combos == [{"python": "3.14"}, {"name": "Mac", "python": "3.14", "os": "macos"}]


def test_matrix_expression_needs_values():
    with pytest.raises(WorkflowError):
        expand_matrix("${{ fromJSON(needs.setup.outputs.matrix) }}")
    ctx = {"needs": {"setup": {"outputs": {"matrix": '{"v": [1, 2]}'}}}}
    assert expand_matrix("${{ fromJSON(needs.setup.outputs.matrix) }}", ctx) == [{"v": 1.0}, {"v": 2.0}]


def test_display_names_follow_github():
    assert job_display_name("test", {}, {"py": "3.12", "os": "linux"}, {}) == "test (3.12, linux)"
    assert job_display_name("test", {"name": "Unit"}, {"py": "3.12"}, {}) == "Unit (3.12)"
    job = {"name": "${{ matrix.name || matrix.python }}"}
    assert job_display_name("tests", job, {"python": "3.14"}, {"matrix": {"python": "3.14"}}) == "3.14"
    assert job_display_name("build", {}, {}, {}) == "build"
    assert step_display_name({"run": "\n  npm ci\n  npm test"}) == "Run npm ci"
    assert step_display_name({"uses": "actions/checkout@v5"}) == "Run actions/checkout@v5"
    assert step_display_name({"name": "Lint", "run": "x"}) == "Lint"


def test_command_file_parsing(tmp_path):
    p = tmp_path / "out"
    p.write_text("a=1\nb=x=y\nnotes<<EOF_9\nline one\nline two\nEOF_9\n\n")
    assert parse_command_file(str(p)) == {"a": "1", "b": "x=y", "notes": "line one\nline two"}
    p.write_text("x<<END\nnever closed\n")
    with pytest.raises(WorkflowError):
        parse_command_file(str(p))
    assert parse_command_file(str(tmp_path / "missing")) == {}


def test_dotenv(tmp_path):
    p = tmp_path / ".secrets"
    p.write_text("# c\nexport TOKEN='abc'\nKEY=\"v=1\"\nPLAIN=x\n")
    assert parse_dotenv(str(p)) == {"TOKEN": "abc", "KEY": "v=1", "PLAIN": "x"}


def test_resolve_target(repo):
    repo.workflow("on: push\njobs:\n  test: {runs-on: x, steps: [{run: 'true'}]}\n")
    repo.workflow("on: push\njobs:\n  test: {runs-on: x, steps: [{run: 'true'}]}\n  lint: {runs-on: x, steps: []}\n",
                  name="other.yml")
    root = str(repo.root)
    assert resolve_target("ci.yml:test", root)[1] == "test"
    assert resolve_target("lint", root)[0].endswith("other.yml")
    with pytest.raises(WorkflowError, match="several workflows"):
        resolve_target("test", root)
    with pytest.raises(WorkflowError, match="no workflow"):
        resolve_target("deploy", root)
