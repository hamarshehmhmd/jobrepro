import os
import shutil
import subprocess
import sys

import pytest

from jobrepro import __version__
from jobrepro.cli import main


def test_version_and_help(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
    assert main([]) == 2


def test_list_shows_jobs_matrix_and_steps(repo, capsys):
    repo.workflow("""
        name: CI
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            strategy:
              matrix: {python: ["3.12", "3.13"]}
            steps:
              - uses: actions/checkout@v5
              - id: t
                run: pytest
          docs:
            uses: ./.github/workflows/docs.yml
    """)
    assert main(["list", "-C", str(repo.root), "--steps"]) == 0
    out = capsys.readouterr().out
    assert "test (3.12)" in out and "2 matrix combinations" in out
    assert "--matrix python=3.13" in out
    assert "2. Run pytest  [id: t]" in out
    assert "reusable workflow" in out


def test_run_exit_codes(repo, capsys):
    repo.workflow("""
        on: push
        jobs:
          ok: {runs-on: x, steps: [{run: "echo fine"}]}
          bad: {runs-on: x, steps: [{run: "exit 4"}]}
    """)
    assert main(["run", "ok", "-C", str(repo.root), "--color", "never"]) == 0
    assert main(["run", "ci.yml:bad", "-C", str(repo.root)]) == 1
    assert main(["run", "missing", "-C", str(repo.root)]) == 2
    assert main(["run", "ok", "-C", str(repo.root), "--matrix", "novalue"]) == 2
    assert "expects KEY=VALUE" in capsys.readouterr().err
    assert main(["plan", "bad", "-C", str(repo.root)]) == 0


def test_secrets_file(repo, tmp_path, capsys):
    sec = tmp_path / "s.env"
    sec.write_text("TOKEN=abc123\n")
    repo.workflow("""
        on: push
        jobs:
          t: {runs-on: x, steps: [{run: "echo tok=${{ secrets.TOKEN }} len=${#TOK}", env: {TOK: "${{ secrets.TOKEN }}"}}]}
    """)
    assert main(["run", "t", "-C", str(repo.root), "--secrets-file", str(sec), "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert "tok=*** len=6" in out and "abc123" not in out


def test_python_dash_m_entry_point(repo):
    repo.workflow("on: push\njobs:\n  t: {runs-on: x, steps: [{run: 'echo via-module'}]}\n")
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    p = subprocess.run([sys.executable, "-m", "jobrepro", "run", "t", "-C", str(repo.root), "--color", "never"],
                       stdout=subprocess.PIPE, text=True, env=env)
    assert p.returncode == 0 and "via-module" in p.stdout


@pytest.mark.skipif(not shutil.which("docker") or os.environ.get("JOBREPRO_SKIP_DOCKER"), reason="needs docker")
def test_container_mode(repo, capsys):
    repo.workflow("""
        on: push
        jobs:
          boxed:
            runs-on: ubuntu-latest
            container: python:3.12-slim
            steps:
              - id: v
                run: python -c "import sys; print('pyver=%d.%d' % sys.version_info[:2])"; echo "x=1" >> "$GITHUB_OUTPUT"
              - run: echo "out=${{ steps.v.outputs.x }} ws=$(test -f .github/workflows/ci.yml && echo mounted)"
    """)
    assert main(["run", "boxed", "-C", str(repo.root), "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert "pyver=3.12" in out and "out=1 ws=mounted" in out
    assert main(["run", "boxed", "-C", str(repo.root), "--image", "alpine:3.20", "--color", "never"]) == 1
