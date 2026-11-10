# r/github draft (post yourself; check the sub's self-promo rules first)

**Title:** I made a CLI that re-runs one GitHub Actions job locally and opens a shell when a step fails

Got sick of push-and-wait just to debug one CI step, so I built jobrepro.

- `jobrepro from-run <failed run url>` gives you the exact local command (job, matrix, step)
- `jobrepro run test --break-on-failure` gives you a shell in the failing step's env, then you retry just that step
- handles ${{ }} expressions, step outputs, GITHUB_ENV/PATH, local composite actions, secret masking

It's not a full act replacement. Remote actions are skipped and it uses your local tools unless you pass `--image`.

https://github.com/hamarshehmhmd/jobrepro (AGPL-3.0, I'm the author)

Feedback welcome, especially workflows where it gets things wrong.
