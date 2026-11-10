# Show HN draft (post yourself: https://news.ycombinator.com/submit)

**Title:** Show HN: Jobrepro – re-run one GitHub Actions job or step locally, with a shell on failure

**URL:** https://github.com/hamarshehmhmd/jobrepro

**Text (first comment):**

I got tired of the edit, push, wait 4 minutes, read the log loop every time one CI step broke.

jobrepro reads your workflow and runs the steps of one job right in your checkout: same ${{ }} expressions, matrix values, GITHUB_ENV/GITHUB_OUTPUT/GITHUB_PATH, step outputs, local composite actions, masking.

The parts I actually use:
- `jobrepro from-run <run url>` finds the failed job, matrix combo and step, then prints the local command
- `--break-on-failure` drops you into a shell with the failing step's env and cwd, you fix things, then retry just that step
- `--break 4` opens a shell before step 4

I tried it on a real red Flask run (mypy on the typing job) and got the same error locally in a few seconds.

It's not a runner emulator like act. Remote JS/Docker actions get skipped with a warning, services aren't started, and by default it uses your local toolchain (`--image` runs the steps in Docker). Python, only dependency is PyYAML, AGPL-3.0.

Would love to hear which workflows break it.
