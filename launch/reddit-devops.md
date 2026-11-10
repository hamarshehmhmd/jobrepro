# r/devops draft (post yourself; r/devops has strict self-promo rules, so post it in their weekly/self-promo thread if one is pinned)

**Title:** Debugging a failed GitHub Actions step without push-and-wait

Small open-source tool I wrote: jobrepro.

You point it at a red run (`jobrepro from-run <url>`), and it tells you the job, matrix combo and failing step, plus the command to re-run it in your checkout. With `--break-on-failure` you land in a shell with that step's exact env and cwd. Fix, exit, retry just that step.

It reproduced a real Flask CI mypy failure locally in a few seconds.

Limits: it's not a runner emulator (remote actions skipped, no services), and it uses local tools by default (`--image` for Docker).

https://github.com/hamarshehmhmd/jobrepro (I'm the author, AGPL-3.0)
