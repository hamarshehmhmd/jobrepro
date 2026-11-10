import base64
import json
import os
import stat
import textwrap

import pytest

from jobrepro.cli import main
from jobrepro.fromrun import command_for, distinguishing_keys, parse_ref, plan_from_run
from jobrepro.workflow import WorkflowError

WF = textwrap.dedent("""
    name: Tests
    on: [push, pull_request]
    jobs:
      tests:
        name: ${{ matrix.name || matrix.python }}
        runs-on: ${{ matrix.os || 'ubuntu-latest' }}
        strategy:
          matrix:
            include:
              - {python: '3.13'}
              - {python: '3.12'}
              - {name: Mac, python: '3.13', os: macos-latest}
        steps:
          - uses: actions/checkout@v5
          - uses: actions/setup-python@v6
            with: {python-version: '${{ matrix.python }}'}
          - name: Run tests
            run: pytest
      lint:
        runs-on: ubuntu-latest
        strategy:
          matrix:
            tool: [ruff, mypy]
        steps:
          - run: echo lint
""")


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    responses = {
        "repos/acme/app/actions/runs/111": {
            "html_url": "https://github.com/acme/app/actions/runs/111", "display_title": "Fix things",
            "event": "pull_request", "head_branch": "fix", "head_sha": "abc1234567890", "conclusion": "failure",
            "path": ".github/workflows/tests.yml",
        },
        "repos/acme/app/actions/runs/111/jobs?filter=latest&per_page=100": {"jobs": [
            {"id": 1, "name": "3.13", "conclusion": "success", "steps": []},
            {"id": 2, "name": "Mac", "conclusion": "failure", "html_url": "https://github.com/acme/app/actions/runs/111/job/2",
             "steps": [{"name": "Set up job", "conclusion": "success"},
                       {"name": "Run actions/checkout@v5", "conclusion": "success"},
                       {"name": "Run tests", "conclusion": "failure"}]},
            {"id": 3, "name": "lint (mypy)", "conclusion": "failure", "steps": [
                {"name": "Run echo lint", "conclusion": "failure"}]},
        ]},
        "repos/acme/app/contents/.github/workflows/tests.yml?ref=abc1234567890": {
            "content": base64.b64encode(WF.encode()).decode()},
    }
    data = tmp_path / "gh.json"
    data.write_text(json.dumps(responses))
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        data = json.load(open(%r))
        path = sys.argv[-1]
        if path not in data:
            sys.stderr.write("HTTP 404: Not Found (" + path + ")\\n"); sys.exit(1)
        print(json.dumps(data[path]))
    """ % str(data)))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(gh.parent) + os.pathsep + os.environ["PATH"])
    return tmp_path


def test_parse_ref():
    assert parse_ref("https://github.com/o/r/actions/runs/42") == ("o/r", 42, None)
    assert parse_ref("https://github.com/o/r/actions/runs/42/job/7") == ("o/r", 42, 7)
    assert parse_ref("https://github.com/o/r/actions/runs/42/attempts/2") == ("o/r", 42, None)
    assert parse_ref("42", "o/r") == ("o/r", 42, None)
    with pytest.raises(WorkflowError):
        parse_ref("42")
    with pytest.raises(WorkflowError):
        parse_ref("https://example.com/nope")


def test_first_failed_job_is_mapped_to_job_matrix_and_step(fake_gh, tmp_path):
    plan = plan_from_run("https://github.com/acme/app/actions/runs/111", workspace=str(tmp_path))
    assert plan["job_id"] == "tests"
    assert plan["matrix"] == {"name": "Mac", "python": "3.13", "os": "macos-latest"}
    assert plan["failed_step"] == 3 and plan["failed_step_name"] == "Run tests"
    cmd = command_for(plan)
    assert cmd == ("jobrepro run .github/workflows/tests.yml:tests --matrix name=Mac --event pull_request "
                   "--until 3 --break-on-failure")


def test_specific_job_url(fake_gh, tmp_path):
    plan = plan_from_run("https://github.com/acme/app/actions/runs/111/job/3", workspace=str(tmp_path))
    assert plan["job_id"] == "lint" and plan["matrix"] == {"tool": "mypy"}
    assert "--matrix tool=mypy" in command_for(plan) and "--until 1" in command_for(plan)
    with pytest.raises(WorkflowError, match="job 9 is not in run"):
        plan_from_run("https://github.com/acme/app/actions/runs/111/job/9", workspace=str(tmp_path))


def test_cli_output_and_json(fake_gh, tmp_path, capsys):
    assert main(["from-run", "https://github.com/acme/app/actions/runs/111", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "job      Mac  ->  .github/workflows/tests.yml:tests" in out
    assert "git fetch origin abc1234567890 && git worktree add ../app-abc12345" in out
    assert main(["from-run", "111", "--repo", "acme/app", "-C", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["failed_step"] == 3 and data["command"].startswith("jobrepro run")
    assert main(["from-run", "999", "--repo", "acme/app", "-C", str(tmp_path)]) == 2
    assert "404" in capsys.readouterr().err


def test_distinguishing_keys():
    combos = [{"os": "linux", "py": "3.12"}, {"os": "linux", "py": "3.13"}, {"os": "mac", "py": "3.13"}]
    assert distinguishing_keys(combos[2], combos) == ["os", "py"] or distinguishing_keys(combos[2], combos) == ["os"]
    assert distinguishing_keys(combos[0], combos) == ["py"]
    assert distinguishing_keys({}, [{}]) == []
