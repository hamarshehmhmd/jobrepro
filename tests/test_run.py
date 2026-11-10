import shutil

import re

import pytest

from jobrepro.workflow import WorkflowError


def has(out, text):
    """True if *text* was printed as a line of step output (not just in a step name)."""
    return re.search(r"^ +%s$" % re.escape(text), out, re.M) is not None

BASIC = """
name: CI
on: [push, pull_request]
env:
  APP: demo
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.12", "3.13"]
    env:
      LEVEL: lvl-${{ matrix.python }}
    steps:
      - uses: actions/checkout@v5
      - id: meta
        run: |
          echo "tag=v-${{ matrix.python }}" >> "$GITHUB_OUTPUT"
          echo "BUILD_DIR=$RUNNER_TEMP/build" >> "$GITHUB_ENV"
          {
            echo 'NOTES<<EOF'
            echo 'first'
            echo 'second'
            echo 'EOF'
          } >> "$GITHUB_ENV"
          mkdir -p "$RUNNER_TEMP/bin" && printf '#!/bin/sh\\necho tool-ran\\n' > "$RUNNER_TEMP/bin/mytool"
          chmod +x "$RUNNER_TEMP/bin/mytool" && echo "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
      - name: Use outputs
        env:
          STEP_ONLY: ${{ steps.meta.outputs.tag }}-step
        run: |
          echo "tag=${{ steps.meta.outputs.tag }} step=$STEP_ONLY app=$APP level=$LEVEL"
          echo "build=${BUILD_DIR##*/} notes=$(echo "$NOTES" | wc -l | tr -d ' ')"
          mytool
          echo "ci=$CI actions=$GITHUB_ACTIONS repo=$GITHUB_REPOSITORY event=$GITHUB_EVENT_NAME job=$GITHUB_JOB"
      - name: Run tests
        run: test "${{ matrix.python }}" = "3.13"
      - if: failure()
        run: echo "cleanup-after-failure"
      - if: always()
        run: echo "always-ran"
      - run: echo "never-after-failure"
"""


def test_outputs_env_path_and_defaults(repo):
    repo.workflow(BASIC)
    res, out = repo.run(matrix={"python": "3.13"})
    assert not res.failed, out
    assert "tag=v-3.13 step=v-3.13-step app=demo level=lvl-3.13" in out
    assert "build=build notes=2" in out
    assert "tool-ran" in out
    assert "ci=true actions=true repo=acme/widgets event=push job=test" in out
    assert not has(out, "cleanup-after-failure")
    assert has(out, "always-ran") and has(out, "never-after-failure")
    assert "skipped: your working tree is the checkout" in out
    assert "job succeeded" in out


def test_failure_then_failure_and_always_steps(repo):
    repo.workflow(BASIC)
    res, out = repo.run(matrix={"python": "3.12"})
    assert res.failed
    assert "failed with exit 1" in out
    assert has(out, "cleanup-after-failure") and has(out, "always-ran")
    assert not has(out, "never-after-failure")
    assert "job failed" in out


def test_matrix_selection_errors_and_first_combo_warning(repo):
    repo.workflow(BASIC)
    with pytest.raises(WorkflowError, match="no matrix combination matches python=2.7"):
        repo.run(matrix={"python": "2.7"})
    res, out = repo.run()
    assert "2 matrix combinations match; running the first" in out
    assert "tag=v-3.12" in out


def test_step_selection(repo):
    repo.workflow(BASIC)
    res, out = repo.run(matrix={"python": "3.13"}, only=["Use outputs"])
    assert "tag= step=-step" in out  # meta did not run, so its output is empty
    assert "(6 steps not selected)" in out
    res, out = repo.run(matrix={"python": "3.13"}, from_step="meta", until_step="2")
    assert "[2] Run echo" in out and "Use outputs" not in out.split("summary")[0]
    res, out = repo.run(matrix={"python": "3.13"}, skip=["Run tests", "6"])
    assert "Run tests" not in out.split("summary")[0] and not has(out, "always-ran")
    with pytest.raises(WorkflowError, match="matches no step"):
        repo.run(matrix={"python": "3.13"}, only=["deploy"])
    with pytest.raises(WorkflowError, match="--from"):
        repo.run(matrix={"python": "3.13"}, from_step="nope")


def test_secrets_are_masked_and_missing_ones_reported(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo "token=${{ secrets.API_TOKEN }}"
                  echo "other=${{ secrets.OTHER }}"
                  echo "::add-mask::runtime-value"
                  echo "late runtime-value"
    """)
    res, out = repo.run(secrets={"API_TOKEN": "sk-live-123"})
    assert "sk-live-123" not in out and "token=***" in out
    assert "runtime-value" not in out and "late ***" in out
    assert "secrets not provided" in out and "OTHER" in out


def test_workflow_commands(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo "::group::Install"
                  echo "inside"
                  echo "::endgroup::"
                  echo "::error file=app.py,line=3::boom"
                  echo "::debug::hidden"
                  echo "::stop-commands::tok42"
                  echo "::error::not a command"
                  echo "::tok42::"
                  echo "::set-output name=x::y"
                  echo "summary line" >> "$GITHUB_STEP_SUMMARY"
    """)
    res, out = repo.run()
    assert has(out, "▸ Install") and not has(out, "::group::Install")
    assert "error (file=app.py,line=3): boom" in out
    assert "hidden" not in out
    assert "::error::not a command" in out
    assert "deprecated ::set-output::" in out
    assert "step summary written to" in out


def test_shells_working_directory_and_defaults(repo):
    repo.write("sub/marker.txt", "x")
    repo.workflow("""
        on: push
        defaults:
          run:
            working-directory: sub
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: ls marker.txt && echo "pipefail=$(set -o | grep pipefail | awk '{print $2}')"
                shell: bash
              - run: |
                  import os
                  print("py-cwd", os.path.basename(os.getcwd()))
                shell: python
              - run: echo "custom $0"
                shell: sh -e {0}
              - run: echo "root=$(basename $PWD)"
                working-directory: .
    """)
    res, out = repo.run()
    assert not res.failed, out
    assert "marker.txt" in out and "pipefail=on" in out
    assert "py-cwd sub" in out
    assert "custom " in out and "step-3.sh" in out
    assert "root=%s" % repo.root.name in out


def test_continue_on_error_and_outcome(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - id: flaky
                continue-on-error: true
                run: exit 3
              - run: echo "outcome=${{ steps.flaky.outcome }} conclusion=${{ steps.flaky.conclusion }}"
    """)
    res, out = repo.run()
    assert not res.failed
    assert "outcome=failure conclusion=success" in out
    assert "failed (continue-on-error)" in out


def test_timeout_minutes(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: sleep 5
                timeout-minutes: 0.01
    """)
    res, out = repo.run()
    assert res.failed and "timeout-minutes exceeded" in out


def test_composite_action_inputs_and_outputs(repo):
    repo.write(".github/actions/greet/action.yml", """
        name: greet
        inputs:
          who: {default: world}
          punct: {default: "!"}
        outputs:
          msg: {value: "${{ steps.g.outputs.msg }}"}
        runs:
          using: composite
          steps:
            - id: g
              shell: bash
              run: echo "msg=hello ${{ inputs.who }}${{ inputs.punct }}" >> "$GITHUB_OUTPUT"
            - shell: bash
              run: echo "action path ok=$(test -f "$GITHUB_ACTION_PATH/action.yml" && echo yes)"
    """)
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: ./.github/actions/greet
                id: greet
                with:
                  who: ci
              - run: echo "said ${{ steps.greet.outputs.msg }}"
    """)
    res, out = repo.run()
    assert not res.failed, out
    assert "said hello ci!" in out and "action path ok=yes" in out


def test_remote_actions_are_skipped_with_warning(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            services:
              db: {image: postgres:16}
            steps:
              - uses: some/deploy-action@v2
              - uses: actions/upload-artifact@v4
              - uses: actions/setup-node@v4
                with: {node-version: "1"}
    """)
    res, out = repo.run()
    assert "remote action some/deploy-action@v2 is not run locally" in out
    assert "artifacts stay in your working tree" in out
    assert "services (db) are not started" in out


def test_inputs_vars_set_and_event(repo, tmp_path):
    ev = tmp_path / "event.json"
    ev.write_text('{"pull_request": {"number": 7, "base": {"ref": "develop"}}}')
    repo.workflow("""
        on:
          workflow_dispatch:
            inputs:
              level: {default: info}
          pull_request:
        jobs:
          test:
            runs-on: ubuntu-latest
            if: github.event_name == 'pull_request'
            steps:
              - run: echo "level=${{ inputs.level }} var=${{ vars.REGION }} pr=${{ github.event.pull_request.number }} base=$GITHUB_BASE_REF"
              - run: echo "needs=${{ needs.build.outputs.tag }}"
              - if: github.event_name == 'push'
                run: echo "push-only"
    """)
    res, out = repo.run(event="pull_request", event_file=str(ev), vars={"REGION": "eu"},
                        sets={"needs.build.outputs.tag": "v9"})
    assert "level=info var=eu pr=7 base=develop" in out
    assert "needs=v9" in out
    assert not has(out, "push-only")
    res, out = repo.run(inputs={"level": "debug"}, event="workflow_dispatch")
    assert "level=debug" in out
    assert "job-level if:" in out


def test_breakpoint_shell_gets_step_env(repo, monkeypatch, tmp_path):
    dump = tmp_path / "dump"
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            env: {JOBVAR: j1}
            steps:
              - run: echo "FROM_STEP1=yes" >> "$GITHUB_ENV"
              - name: Deploy
                env: {STEPVAR: s2}
                run: echo deploying
    """)
    monkeypatch.setenv("JOBREPRO_SHELL", "sh -c 'echo $JOBVAR $STEPVAR $FROM_STEP1 $(basename $JOBREPRO_STEP_SCRIPT) > %s'" % dump)
    res, out = repo.run(breaks=["Deploy"])
    assert dump.read_text().split() == ["j1", "s2", "yes", "step-2.sh"]
    assert has(out, "deploying") and not res.failed
    monkeypatch.setenv("JOBREPRO_SHELL", "sh -c 'exit 1'")
    res, out = repo.run(breaks=["2"])
    assert "stopped at breakpoint" in out and not has(out, "deploying")


def test_break_on_failure_retry_after_fix(repo, monkeypatch):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: needs config
                run: test -f config.ini && echo "config found"
              - run: echo "next step"
    """)
    # The "developer" fixes the problem inside the debug shell, then retries.
    monkeypatch.setenv("JOBREPRO_SHELL", "sh -c 'touch config.ini'")
    res, out = repo.run(break_on_failure=True, stdin="r\n")
    assert "Opening a shell" in out
    assert has(out, "config found") and has(out, "next step") and not res.failed
    (repo.root / "config.ini").unlink()
    monkeypatch.setenv("JOBREPRO_SHELL", "true")
    res, out = repo.run(break_on_failure=True, stdin="")
    assert "(no answer: abort)" in out and not has(out, "next step")


def test_dry_run_executes_nothing(repo):
    repo.workflow("""
        on: push
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: touch created-by-step && echo "py=${{ matrix.python || 'none' }}"
    """)
    res, out = repo.run(dry_run=True)
    assert not (repo.root / "created-by-step").exists()
    assert "│ touch created-by-step && echo \"py=none\"" in out
    assert "dry run: nothing was executed" in out


def test_reusable_and_container_jobs(repo, monkeypatch):
    repo.workflow("""
        on: push
        jobs:
          call:
            uses: ./.github/workflows/other.yml
          boxed:
            runs-on: ubuntu-latest
            container: node:22
            steps:
              - run: node --version
    """)
    with pytest.raises(WorkflowError, match="reusable workflow"):
        repo.run(job="call")
    monkeypatch.setenv("PATH", "/nonexistent")
    if shutil.which("docker"):
        pytest.skip("docker present")
    with pytest.raises(WorkflowError, match="docker is not on PATH"):
        repo.run(job="boxed")
