import io
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobrepro.runner import JobRunner, Options  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo; write(path, text) adds files."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                   check=True)

    class R:
        root = tmp_path

        def write(self, rel, text):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text))
            return p

        def workflow(self, text, name="ci.yml"):
            return self.write(".github/workflows/" + name, text)

        def run(self, job="test", stdin="", **kw):
            out = io.StringIO()
            kw.setdefault("color", False)
            opts = Options(workspace=str(tmp_path), out=out, inp=io.StringIO(stdin), **kw)
            path = str(tmp_path / ".github" / "workflows" / kw.pop("wf", "ci.yml"))
            res = JobRunner(path, job, opts).run()
            return res, out.getvalue()

    return R()
