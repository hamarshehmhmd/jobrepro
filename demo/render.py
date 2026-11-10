"""Render the jobrepro narrated demo: real command output + edge-tts narration.

Usage: python demo/render.py   (needs ffmpeg, edge-tts, Pillow, network)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
SDKBIN = "/tmp/jrvenv/bin"
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80),
        "2": (130, 135, 145), "36": (90, 200, 230), "35": (200, 140, 230), "1": FG}
PROMPT = "dev@laptop $ "


FLASK = "/tmp/jrdemo/flask"
SHOP = "/tmp/jrdemo/shop"
FLASK_SHA = "329c32b916938d33fec41f40eb7ed854bf0b3e2b"
NOISE = " 2>&1 | grep -v -e 'job control' -e 'terminal process group'"

SCENES = [
    {
        "kind": "title",
        "title": "jobrepro",
        "sub": "re-run one GitHub Actions job or step on your machine,\nwith a shell in the failing step. No push-and-wait.",
        "say": "This is job repro, an open source command line tool that re-runs one GitHub Actions job, or one step of it, "
               "on your own machine, and drops you into a shell in the failing step's environment.",
    },
    {
        "kind": "bullets",
        "title": "The problem",
        "bullets": [
            "CI fails: edit YAML, push, wait minutes, read logs, repeat",
            "\"The Pain That Is GitHub Actions\": 704 points on HN",
            "\"How can I run GitHub Actions workflows locally?\"",
            "   324k views on Stack Overflow",
            "tmate SSH debugging stopped working: its servers shut down",
        ],
        "say": ("When a CI step fails, the debug loop is: edit the workflow, push, wait a few minutes, read the log, and "
                "repeat. The pain that is GitHub Actions got seven hundred points on Hacker News, the Stack Overflow "
                "question on running workflows locally has three hundred twenty four thousand views, and the old trick of "
                "S S H into the runner with tmate stopped working when its servers shut down."),
    },
    {
        "kind": "term",
        "cwd": FLASK,
        "prompt": "dev@laptop flask $ ",
        "cmds": ["jobrepro from-run https://github.com/pallets/flask/actions/runs/37633292268"],
        "say": ("Here's a real failed run on the Flask repository. job repro from run takes the run U R L, uses the "
                "GitHub C L I to find the failed job, its matrix values and the step that broke, and prints the exact "
                "local command. My checkout is already at the failing commit."),
    },
    {
        "kind": "term",
        "cwd": FLASK,
        "prompt": "dev@laptop flask $ ",
        "tail": True,
        "cmds": ["jobrepro run .github/workflows/tests.yaml:typing --until 5"],
        "say": ("Running it executes the typing job's steps right here. Checkout and cache are skipped, setup steps are "
                "checked against my local tools, and the tox step runs for real. In a few seconds I get the same "
                "my pie error that failed on GitHub, without pushing anything."),
    },
    {
        "kind": "term",
        "prompt": "dev@laptop shop $ ",
        "cmds": ["jobrepro list --steps"],
        "say": ("In a smaller project, job repro list shows every job, its matrix combinations, and numbered steps. "
                "You can pick steps by number, I D, or name."),
    },
    {
        "kind": "term",
        "prompt": "dev@laptop shop $ ",
        "cmds": ["jobrepro run test -m python=3.13 --secret API_TOKEN=sk-live-123"],
        "say": ("Running the test job evaluates expressions like the runner does. Step outputs and GITHUB ENV flow between "
                "steps, the local composite action runs with its inputs and outputs, the secret is masked as stars, and the "
                "error annotation shows that config dot ini is missing."),
    },
    {
        "kind": "term",
        "prompt": "dev@laptop shop $ ",
        "cmds": ["printf 'ls\\nprintf \"[shop]\\\\ncurrency = EUR\\\\n\" > config.ini\\nexit\\nr\\n' | "
                 "jobrepro run test -m python=3.13 --only 'Run tests' -B" + NOISE],
        "show": ["jobrepro run test -m python=3.13 --only 'Run tests' --break-on-failure"],
        "say": ("With break on failure, a failing step opens a shell with that step's exact environment and working "
                "directory. I create the missing config file, exit, and choose retry. Only that step runs again, and now "
                "it passes. You can also set a breakpoint before any step with dash dash break."),
    },
    {
        "kind": "term",
        "prompt": "dev@laptop shop $ ",
        "cmds": ["jobrepro plan test -m python=3.12 --from 3 --until 5"],
        "say": ("And plan is a dry run: it prints each rendered script, its shell and its environment, so you can see "
                "exactly what would run."),
    },
    {
        "kind": "bullets",
        "title": "Limits",
        "bullets": [
            "Not a full runner: remote JS/Docker actions are skipped",
            "services: are not started; needs outputs via --set",
            "Uses your local toolchain; --image runs steps in Docker",
            "Linux and macOS; Windows runners not a target yet",
            "For whole-workflow emulation, see nektos/act",
        ],
        "say": ("The limits. It is not a full runner emulator: remote JavaScript and Docker actions are skipped with a "
                "warning, and services are not started. Steps use your machine's tools unless you pass dash dash image to "
                "run them in a container. For whole workflow emulation, use act."),
    },
    {
        "kind": "bullets",
        "title": "Try it",
        "bullets": [
            "curl -fsSL https://raw.githubusercontent.com/",
            "   hamarshehmhmd/jobrepro/main/install.sh | sh",
            "jobrepro from-run <run URL>  ·  jobrepro run JOB -B",
            "github.com/hamarshehmhmd/jobrepro",
            "GNU AGPL-3.0-or-later (commercial licenses available)",
        ],
        "say": ("Install it with the one line installer, and next time CI goes red, run job repro from run. It's licensed "
                "under the GNU Affero General Public License version 3 or later, with commercial licenses available "
                "from the author."),
    },
]


async def tts(text, path):
    import edge_tts
    for attempt in range(8):
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 7:
                raise
            await asyncio.sleep(5)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    )
    return float(out.strip())


EXPORTS = {}


def run_cmd(cmd, cwd):
    if cmd.startswith("export ") and "=" in cmd:
        k, v = cmd[7:].split("=", 1)
        EXPORTS[k] = v
        return ""
    env = dict(os.environ, FORCE_COLOR="1", **EXPORTS)
    env.pop("NO_COLOR", None)
    env["PATH"] = SDKBIN + os.pathsep + env["PATH"]
    env["COLUMNS"] = "120"
    p = subprocess.run(
        ["bash", "-c", "shopt -s nullglob; " + cmd],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    return p.stdout.rstrip("\n")


def parse_ansi(line):
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 14)
        self.bold = ImageFont.truetype(MONO_B, 14)
        self.cw = self.font.getlength("M")
        self.lh = 20
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if room <= 4:
                    lines.append(cur); cur, n = [], 0
                    text = "    " + text.lstrip(" ")
                    room = self.cols
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 4 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~ — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-30:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 78), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 32), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/jobrepro  ·  from-run · run · --break · --break-on-failure · plan  ·  GNU AGPL-3.0",
               font=ImageFont.truetype(SANS, 20), fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
             "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-shortest", path],
            stdin=subprocess.PIPE,
        )
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def _cap_output(out, tail=False):
    lines = out.splitlines()
    if tail and len(lines) > 26:
        return "\n".join(["…"] + lines[-25:])
    if len(lines) > 24:
        lines = lines[:22] + ["…", lines[-1]]
    return "\n".join(lines)


def render_scene(i, scene, workdir):
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    wd = workdir
    outputs = [_cap_output(run_cmd(c, wd), scene.get("tail")) for c in scene["cmds"]]
    # If show has more lines than cmds (a command displayed as several visual lines
    # that map to one executed command), join show into one typed command per output.
    if len(shown) != len(outputs):
        if len(outputs) == 1:
            shown = [" ".join(s.rstrip("\\").strip() for s in shown)]
        elif len(shown) > len(outputs):
            # first N-1 outputs map 1:1; remaining show lines are the last command
            shown = list(shown[: len(outputs) - 1]) + [
                " ".join(s.rstrip("\\").strip() for s in shown[len(outputs) - 1 :])
            ]
    steps = list(zip(shown, outputs))
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(scene.get("prompt", PROMPT), (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(scene.get("prompt", PROMPT), (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path





def setup():
    if not os.path.isdir(os.path.join(FLASK, ".git")):
        os.makedirs("/tmp/jrdemo", exist_ok=True)
        subprocess.run(["git", "clone", "-q", "https://github.com/pallets/flask", FLASK], check=True)
    subprocess.run(["git", "-C", FLASK, "checkout", "-q", FLASK_SHA], check=True)
    if os.path.exists(SHOP):
        shutil.rmtree(SHOP)
    shutil.copytree(os.path.join(ROOT, "examples", "demo-repo"), SHOP)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=dev@example.com", "-c", "user.name=dev",
                                                  "commit", "-qm", "shop"]):
        subprocess.run(["git", "-C", SHOP] + args, check=True)


def main():
    os.makedirs(BUILD, exist_ok=True)
    setup()
    only = [int(x) for x in sys.argv[1:]]
    parts = [render_scene(i, s, s.get("cwd", SHOP)) if (not only or i in only) else os.path.join(BUILD, "s%d.mp4" % i)
             for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
         os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT],
        check=True,
    )
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()
