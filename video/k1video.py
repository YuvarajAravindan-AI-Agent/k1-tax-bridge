"""Render the K-1 bridge LinkedIn demo: 1080x1080, narration, burned-in captions.

Style 'ledger-paper-audit' (see style.json; registered in the video style registry).
Live footage = real asciinema recordings made on the Contabo box (casts/*.cast),
replayed through a terminal emulator with idle gaps shortened. Nothing in a shell
panel is retyped or edited.

    python k1video.py            -> out/k1-bridge-demo.mp4 (+ -preview.mp4, .srt)
"""
import json, math, os, random, subprocess, sys, wave

import imageio_ffmpeg, pyte
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, "/opt/ai-digest")
import render  # noqa: E402  (shared gateway TTS with cache; key stays in /opt/ai-digest/.env)

render.VOICE = "Orus"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
W = H = 1080
FPS = 30
P_SEG, P_SCENE = 0.25, 0.55

PAPER, RULE, MARGIN = (246, 241, 226), (205, 214, 222), (214, 120, 112)
INK, MUTED = (34, 38, 46), (112, 108, 98)
GREEN, RED = (28, 110, 72), (186, 42, 42)
TAPE = (252, 250, 243)
SHELL_BG, SHELL_FG, SHELL_GREEN = (30, 35, 32), (232, 228, 214), (120, 200, 140)


def F(name, size):
    return ImageFont.truetype(os.path.join(HERE, "fonts", name), size)


TYPE = lambda s: F("CourierPrime-Regular.ttf", s)
TYPEB = lambda s: F("CourierPrime-Bold.ttf", s)
HEAD = lambda s: F("ArchivoBlack-Regular.ttf", s)

# ------------------------------------------------------------------ script
SCENES = [
    dict(id="hook", label="HOOK", segs=[
        "Same partnership K-1. Same open-source tax engine.",
        "One pipeline reported one point six million dollars of income.",
        "The K-1 actually supports three point zero two million."],
        caps=["Same partnership K-1. Same open-source tax engine.",
              "One pipeline reported $1.6M of income.",
              "The K-1 actually supports $3.02M."]),
    dict(id="problem", label="PROBLEM", segs=[
        "K-1s arrive as PDFs. An open standard now turns them into structured data, "
        "and an open-source engine computes the 1040.",
        "But when a form lacks evidence, the engine drops it, logs a warning, and still answers."]),
    dict(id="gates", label="GATES", segs=[
        "So I built a governed bridge. Seven fail-closed gates.",
        "Validate the document, match the masked IDs, account for every value, "
        "and check required answers up front.",
        "Then confirm the engine took every entry, dropped nothing, "
        "and that each 1040 line reconciles to the K-1."]),
    dict(id="naive", label="RUN 1", cast="naive", rows=6, segs=[
        "Live, on my server. Run one: a plain wrapper.",
        "One point six million. Two warnings, never read."],
        caps=[None, "$1.6M. Two warnings, never read."]),
    dict(id="blocked", label="RUN 2", cast="blocked", rows=8, segs=[
        "Run two is governed, with the Schedule B answers withheld.",
        "Blocked at gate four, before the engine even runs."]),
    dict(id="governed", label="RUN 3", cast="governed", rows=18, segs=[
        "Run three, with the full preparer facts. Three point zero two million.",
        "Interest, dividends, capital gains and Schedule E income each match the K-1 to the dollar.",
        "All thirty-five K-1 values are accounted for: sent, attested, or excluded by a named "
        "reviewer. Verdict: computed with exclusions, not complete."],
        caps=["Run three, with the full preparer facts. $3.02M.", None,
              "All 35 K-1 values accounted for: sent, attested, or excluded by a named reviewer. "
              "Verdict: computed with exclusions, not complete."]),
    dict(id="infra", label="INFRA", cast="infra", rows=18, hold=2.8, segs=[
        "It all runs on a self-hosted Contabo VPS.",
        "The engine is compiled from source into one binary, and thirty-five tests pin every gate.",
        "Synthetic data only. A governance demo, not a filing tool."],
        caps=[None, "The engine is compiled from source into one binary, and 35 tests pin every gate.",
              None]),
    dict(id="takeaway", label="POINT", segs=[
        "Open engines make agentic tax work possible. Governance makes it trustworthy."]),
    dict(id="end", label="SIGNED", segs=[
        "I'm Yuvaraj Aravindan, founder of AI Agentic Enterprises. "
        "We build governed AI agents for regulated industries."]),
]


# ------------------------------------------------------------------ casts
def load_cast(name, max_gap=0.9):
    """Events with idle gaps capped; returns [(t, data)], duration."""
    lines = open(os.path.join(HERE, "casts", f"{name}.cast")).read().splitlines()[1:]
    ev, t_prev, t_new = [], 0.0, 0.0
    for ln in lines:
        t, kind, data = json.loads(ln)
        if kind != "o":
            continue
        t_new += min(t - t_prev, max_gap)
        t_prev = t
        ev.append((t_new, data))
    return ev, t_new


class ShellReplay:
    def __init__(self, name, cols=64, rows=20):
        self.events, self.duration = load_cast(name)
        self.cols, self.rows = cols, rows

    def screen_at(self, t):
        scr = pyte.Screen(self.cols, self.rows)
        st = pyte.Stream(scr)
        for et, data in self.events:
            if et > t:
                break
            st.feed(data)
        return scr


# ------------------------------------------------------------------ audio + timeline
def build_timeline():
    params, chunks, t = None, [], 0.0
    srt = []
    for sc in SCENES:
        sc["start"] = t
        sc["seg_times"] = []
        caps = sc.get("caps") or [None] * len(sc["segs"])
        for i, seg in enumerate(sc["segs"]):
            p, frames = render.read_wav(render.tts(seg))
            params = params or p
            dur = len(frames) / (p.sampwidth * p.nchannels) / p.framerate
            sc["seg_times"].append((t, t + dur, caps[i] or seg))
            srt.append((t, t + dur, caps[i] or seg))
            chunks.append(frames)
            t += dur
            gap = P_SEG if i < len(sc["segs"]) - 1 else 0
            chunks.append(b"\0" * int(gap * p.framerate) * p.sampwidth * p.nchannels)
            t += gap
        if sc.get("cast"):
            sc["shell"] = ShellReplay(sc["cast"], rows=sc.get("rows", 20))
            need = sc["start"] + 0.6 + sc["shell"].duration + sc.get("hold", 1.2)
            if need > t:
                pad = need - t
                chunks.append(b"\0" * int(pad * params.framerate) * params.sampwidth * params.nchannels)
                t += pad
        tail = 1.4 if sc["id"] == "end" else P_SCENE
        chunks.append(b"\0" * int(tail * params.framerate) * params.sampwidth * params.nchannels)
        t += tail
        sc["end"] = t
    os.makedirs(OUT, exist_ok=True)
    wav = os.path.join(OUT, "narration.wav")
    with wave.open(wav, "wb") as w:
        w.setparams(params)
        w.writeframes(b"".join(chunks))
    with open(os.path.join(OUT, "k1-bridge-demo.srt"), "w") as f:
        for i, (a, b, text) in enumerate(srt, 1):
            ts = lambda x: f"{int(x // 3600):02}:{int(x % 3600 // 60):02}:{int(x % 60):02},{int(x % 1 * 1000):03}"
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n\n")
    return wav, t


# ------------------------------------------------------------------ drawing helpers
random.seed(7)
NOISE = Image.effect_noise((W, H + 80), 18).convert("L").filter(ImageFilter.GaussianBlur(0.6))


def paper(t):
    img = Image.new("RGB", (W, H + 80), PAPER)
    img = Image.composite(img, Image.new("RGB", img.size, (232, 226, 208)), NOISE.point(lambda v: max(0, min(255, 255 - (v - 110) // 3))))
    d = ImageDraw.Draw(img)
    for y in range(0, H + 80, 40):
        d.line((0, y, W, y), fill=RULE, width=1)
    d.line((92, 0, 92, H + 80), fill=MARGIN, width=2)
    d.line((98, 0, 98, H + 80), fill=MARGIN, width=1)
    off = int(t * 7) % 40  # slow vertical drift down the page
    return img.crop((0, off, W, off + H))


PAPER_FRAMES = {}


def paper_at(t):
    k = int(t * 7) % 40
    if k not in PAPER_FRAMES:
        PAPER_FRAMES[k] = paper(k / 7)
    return PAPER_FRAMES[k].copy()


def wrap(d, text, font, width):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=font) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def stamp(img, text, color, center, t_since, angle=-8, size=58, sub=None):
    """Rubber stamp that thumps down (scale 1.35 -> 1) and stays."""
    if t_since < 0:
        return
    f = HEAD(size)
    fs = TYPEB(26)
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    w = int(max(tmp.textlength(text, font=f), tmp.textlength(sub or "", font=fs))) + 60
    h = size + 50 + (36 if sub else 0)
    s = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(s)
    col = color + (225,)
    d.rounded_rectangle((3, 3, w - 4, h - 4), radius=10, outline=col, width=6)
    d.rounded_rectangle((13, 13, w - 14, h - 14), radius=6, outline=col, width=2)
    d.text((w / 2, 22 + size / 2), text, font=f, fill=col, anchor="mm")
    if sub:
        d.text((w / 2, h - 36), sub, font=fs, fill=col, anchor="mm")
    # ink texture: knock out speckles so it reads as a stamp
    speck = Image.effect_noise((w, h), 60).point(lambda v: 0 if v > 175 else 255)
    s.putalpha(Image.composite(s.getchannel("A"), Image.new("L", (w, h), 0), speck))
    k = 1.35 - 0.35 * ease(t_since / 0.18)
    s = s.resize((int(w * k), int(h * k)), Image.LANCZOS).rotate(angle, expand=True, resample=Image.BICUBIC)
    img.paste(s, (int(center[0] - s.width / 2), int(center[1] - s.height / 2)), s)


def caption(img, d, sc, t):
    for a, b, text in sc["seg_times"]:
        if a - 0.05 <= t <= b + (P_SEG if b < sc["seg_times"][-1][1] else 0.3):
            break
    else:
        return
    f = TYPE(31)
    lines = wrap(d, text, f, 900)[:3]
    total = sum(len(l) for l in lines)
    shown = int(total * ease((t - a) / max(0.6, (b - a) * 0.55)))
    h = 28 + 40 * len(lines)
    tape = Image.new("RGBA", (980, h), TAPE + (245,))
    td = ImageDraw.Draw(tape)
    for x in range(0, 980, 14):  # torn edge
        td.polygon([(x, h), (x + 7, h - 5), (x + 14, h)], fill=(0, 0, 0, 0))
    sh = Image.new("RGBA", (980, h), (0, 0, 0, 60)).filter(ImageFilter.GaussianBlur(4))
    img.paste(sh, (54, 30), sh)
    img.paste(tape, (50, 24), tape)
    y, left = 38, shown
    for ln in lines:
        part = ln[:max(0, left)]
        left -= len(ln)
        d.text((76, y), part, font=f, fill=INK)
        y += 40
    return h


def progress(d, idx):
    n = len(SCENES)
    f = TYPE(16)
    x0, gap, y = 118, (W - 150) / n, 1026
    for i, sc in enumerate(SCENES):
        x = x0 + i * gap
        box = (x, y, x + 22, y + 22)
        d.rectangle(box, outline=INK if i <= idx else MUTED, width=2)
        if i < idx:
            d.line((x + 4, y + 11, x + 9, y + 17), fill=GREEN, width=3)
            d.line((x + 9, y + 17, x + 19, y + 3), fill=GREEN, width=3)
        elif i == idx:
            d.rectangle((x + 6, y + 6, x + 16, y + 16), fill=RED)
        d.text((x + 27, y + 11), sc["label"], font=f, fill=INK if i == idx else MUTED, anchor="lm")


def shell_panel(img, sc, t, top=230, rows_px=30):
    shell = sc["shell"]
    scr = shell.screen_at(t - sc["start"] - 0.6)
    x0, w = 40, 1000
    h = 46 + shell.rows * rows_px + 18
    sh = Image.new("RGBA", (w, h), (0, 0, 0, 70)).filter(ImageFilter.GaussianBlur(6))
    img.paste(sh, (x0 + 6, top + 8), sh)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, top, x0 + w, top + h), radius=8, fill=SHELL_BG)
    d.text((x0 + 150, top + 23), "root@contabo  ·  live recording", font=TYPE(20), fill=(150, 160, 150), anchor="lm")
    for i, c in enumerate(((220, 90, 80), (225, 180, 70), (110, 180, 100))):
        d.ellipse((x0 + w - 90 + i * 26, top + 15, x0 + w - 74 + i * 26, top + 31), fill=c)
    # pinned with tape strips
    for cx in (x0 + 70, x0 + w - 70):
        tp = Image.new("RGBA", (110, 30), (240, 232, 200, 200)).rotate(-6 if cx < 500 else 6, expand=True)
        img.paste(tp, (cx - 55, top - 16), tp)
    f, fb = TYPE(25), TYPEB(25)
    cw = d.textlength("M", font=f)
    for r in range(shell.rows):
        line = scr.buffer[r]
        y = top + 46 + r * rows_px
        for c in range(shell.cols):
            ch = line[c]
            if ch.data.strip():
                col = SHELL_GREEN if ch.fg == "green" else SHELL_FG
                d.text((x0 + 18 + c * cw, y), ch.data, font=fb if ch.bold else f, fill=col)
    if int(t * 2) % 2 == 0:  # blinking cursor
        cx, cy = scr.cursor.x, scr.cursor.y
        d.rectangle((x0 + 18 + cx * cw, top + 50 + cy * rows_px, x0 + 18 + (cx + 1) * cw, top + 46 + (cy + 1) * rows_px - 4), fill=SHELL_FG)
    return top + h


def flow_box(d, x, y, w, h, title, sub, col=INK):
    d.rounded_rectangle((x, y, x + w, y + h), radius=10, outline=col, width=3, fill=(250, 247, 236))
    d.text((x + w / 2, y + h / 2 - 16), title, font=HEAD(26), fill=col, anchor="mm")
    d.text((x + w / 2, y + h / 2 + 20), sub, font=TYPE(20), fill=MUTED, anchor="mm")


def arrow(d, x1, y, x2, col=INK):
    d.line((x1, y, x2 - 10, y), fill=col, width=4)
    d.polygon([(x2, y), (x2 - 16, y - 9), (x2 - 16, y + 9)], fill=col)


GATES = ["Document passes the K-1 validator", "IDs match the masked K-1 digits",
         "Every K-1 value gets a disposition", "Required answers present (preflight)",
         "Engine accepted every entry", "No form silently dropped",
         "1040 lines reconcile to the K-1"]


# ------------------------------------------------------------------ scenes
def draw_scene(sc, t):
    img = paper_at(t)
    d = ImageDraw.Draw(img)
    lt = t - sc["start"]
    seg = [a for a, _, _ in sc["seg_times"]]
    sid = sc["id"]
    if sid == "hook":
        d.text((540, 330), "SAME K-1.  SAME ENGINE.", font=HEAD(46), fill=INK, anchor="mm")
        if t >= seg[1] - 0.1:
            a = ease((t - seg[1]) / 0.5)
            d.text((540, 470), "pipeline A reported", font=TYPE(30), fill=MUTED, anchor="mm")
            d.text((540, 560), "$1,598,400", font=HEAD(112), fill=RED if t >= seg[2] else INK, anchor="mm")
            if t >= seg[2]:  # red-ink strike-through drawn across
                k = ease((t - seg[2]) / 0.5)
                d.line((210, 575, 210 + 660 * k, 548), fill=RED, width=9)
        if t >= seg[2]:
            d.text((540, 690), "the K-1 supports", font=TYPE(30), fill=MUTED, anchor="mm")
            d.text((540, 790), "$3,024,900", font=HEAD(112), fill=GREEN, anchor="mm")
    elif sid == "problem":
        y = 330
        flow_box(d, 70, y, 270, 130, "K-1 PDF", "partnership issues")
        arrow(d, 345, y + 65, 400)
        flow_box(d, 405, y, 270, 130, "OPEN K-1 DATA", "structured standard")
        arrow(d, 680, y + 65, 735)
        flow_box(d, 740, y, 270, 130, "1040 ENGINE", "open source")
        if t >= seg[1]:
            d.text((540, 560), "when a form lacks evidence, the engine…", font=TYPE(30), fill=INK, anchor="mm")
            for i, (word, delay) in enumerate((("DROPS IT", 1.6), ("WARNS", 3.0), ("STILL ANSWERS", 4.6))):
                if t >= seg[1] + delay:
                    d.text((540, 640 + i * 90), word, font=HEAD(64), fill=RED if i == 2 else INK, anchor="mm")
        if t >= seg[1] + 5.6:
            stamp(img, "CONFIDENT. WRONG.", RED, (770, 880), t - seg[1] - 5.6, angle=-10, size=44)
    elif sid == "gates":
        d.text((120, 250), "GOVERNED BRIDGE  ·  7 GATES  ·  FAIL-CLOSED", font=HEAD(32), fill=INK)
        tick_from = seg[1] if len(seg) > 1 else seg[0]
        span = (sc["seg_times"][-1][1] - tick_from) / len(GATES)
        for i, g in enumerate(GATES):
            y = 330 + i * 86
            d.rectangle((130, y, 168, y + 38), outline=INK, width=3)
            on = t >= tick_from + (i + 0.6) * span
            if on:
                d.line((137, y + 19, 147, y + 30), fill=GREEN, width=5)
                d.line((147, y + 30, 163, y + 6), fill=GREEN, width=5)
            d.text((190, y + 19), f"G{i + 1}", font=HEAD(30), fill=GREEN if on else MUTED, anchor="lm")
            d.text((255, y + 19), g, font=TYPE(31), fill=INK if on else MUTED, anchor="lm")
    elif sid in ("naive", "blocked", "governed"):
        bottom = shell_panel(img, sc, t)
        end = sc["start"] + 0.6 + sc["shell"].duration
        if sid == "naive":
            if t >= seg[1]:
                d.text((60, bottom + 40), "−$1,426,500 vs the K-1", font=HEAD(44), fill=RED)
                rows = [("Schedule B dropped", "−$687,000"), ("box 11S never sent", "−$738,700"),
                        ("royalties skipped", "−$800")]
                for i, (k, v) in enumerate(rows):
                    if t >= seg[1] + 0.8 + 0.5 * i:
                        y = bottom + 120 + i * 50
                        d.text((60, y), k, font=TYPE(30), fill=INK)
                        d.text((620, y), v, font=TYPEB(30), fill=RED)
                        d.line((60, y + 42, 820, y + 42), fill=RULE, width=1)
            stamp(img, "REPORTED ANYWAY", RED, (700, 880), t - max(seg[1] + 2.4, end - 0.3), angle=-9, size=46)
        elif sid == "blocked":
            if t >= seg[1]:
                d.text((60, bottom + 40), "No result reported,", font=HEAD(44), fill=GREEN)
                d.text((60, bottom + 100), "so no wrong number exists.", font=HEAD(44), fill=GREEN)
            stamp(img, "BLOCKED · G4", RED, (720, 860), t - seg[1] - 0.6, angle=-7, size=60)
        else:
            if t >= seg[1] - 0.2:
                d.text((60, bottom + 22), "5 of 5 lines match the K-1 to the dollar", font=TYPEB(27), fill=GREEN)
            if len(seg) > 2 and t >= seg[2]:
                d.text((60, bottom + 58), "35 values · 0 unaccounted · 15 named exclusions", font=TYPE(26), fill=INK)
            stamp(img, "RECONCILED", GREEN, (820, 700), t - seg[1] - 1.2, angle=-8, size=50)
    elif sid == "infra":
        bottom = shell_panel(img, sc, t, top=230, rows_px=27)
        chips = ["Contabo VPS · 4 vCPU · 8 GB", "one 148 MB engine binary",
                 "isolated Python venv", "35 tests pin 7 gates"]
        for i, c in enumerate(chips):
            if t >= seg[min(i, len(seg) - 1)] + (0 if i < 2 else 2.5 * (i - 1)):
                x, y = 40 + (i % 2) * 505, bottom + 16 + (i // 2) * 50
                d.rounded_rectangle((x, y, x + 495, y + 42), radius=6, outline=INK, width=2, fill=(250, 247, 236))
                d.text((x + 247, y + 21), c, font=TYPEB(22), fill=INK, anchor="mm")
        if len(seg) > 2 and t >= seg[2]:
            stamp(img, "SYNTHETIC DATA", RED, (700, 650), t - seg[2] - 0.3, angle=7, size=38,
                  sub="governance demo · not a filing tool")
    elif sid == "takeaway":
        d.text((540, 420), "Open engines make", font=HEAD(58), fill=INK, anchor="mm")
        d.text((540, 500), "agentic tax work possible.", font=HEAD(58), fill=INK, anchor="mm")
        if lt > 2.6:
            d.text((540, 650), "Governance makes it", font=HEAD(58), fill=GREEN, anchor="mm")
            d.text((540, 730), "trustworthy.", font=HEAD(58), fill=GREEN, anchor="mm")
    elif sid == "end":
        d.text((120, 260), "AUDIT SIGN-OFF", font=HEAD(40), fill=INK)
        rows = [("Prepared by", "Yuvaraj Aravindan"), ("Role", "Founder, AI Agentic Enterprises"),
                ("Practice", "Governed AI agents for regulated industries"), ("Web", "ai-agentic-enterprises.com")]
        for i, (k, v) in enumerate(rows):
            y = 360 + i * 80
            d.text((120, y), k.upper(), font=TYPE(22), fill=MUTED)
            d.text((120, y + 28), v, font=TYPEB(31), fill=INK)
            d.line((120, y + 72, 960, y + 72), fill=INK, width=1)
        k = ease(lt / 1.6)  # signature written across the line
        pts = [(130 + i * 6, 740 - 18 * math.sin(i / 4.5) - 8 * math.sin(i / 1.7)) for i in range(int(120 * k))]
        if len(pts) > 1:
            d.line(pts, fill=(20, 40, 110), width=4, joint="curve")
        d.line((120, 770, 960, 770), fill=INK, width=2)
        d.text((120, 782), "signature", font=TYPE(20), fill=MUTED)
        stamp(img, "GOVERNED", GREEN, (760, 880), lt - 1.8, angle=-10, size=52)
    return img


def scene_index(t, scenes):
    return max(i for i, s in enumerate(scenes) if s["start"] <= t)


def frame(t, scenes, prev_cache):
    idx = scene_index(t, scenes)
    sc = scenes[idx]
    img = draw_scene(sc, t)
    d = ImageDraw.Draw(img)
    caption(img, d, sc, t)
    progress(d, idx)
    lt = t - sc["start"]
    if idx > 0 and lt < 0.45:  # page slides up over the previous scene's last frame
        prev = prev_cache.get(idx - 1)
        if prev is not None:
            y = int(H * (1 - ease(lt / 0.45)))
            base = prev.copy()
            shadow = Image.new("RGBA", (W, 30), (0, 0, 0, 80)).filter(ImageFilter.GaussianBlur(8))
            base.paste(img.crop((0, 0, W, H - y)), (0, y))
            if y > 20:
                base.paste(shadow, (0, y - 24), shadow)
            img = base
    return img, idx


def main():
    wav, total = build_timeline()
    print(f"timeline {total:.1f}s; scenes:", ", ".join(f"{s['id']}@{s['start']:.1f}" for s in SCENES), flush=True)
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    mp4 = os.path.join(OUT, "k1-bridge-demo.mp4")
    p = subprocess.Popen([ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                          "-r", str(FPS), "-i", "-", "-i", wav, "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", mp4],
                         stdin=subprocess.PIPE)
    prev_cache, last_idx, last_img = {}, 0, None
    n = int(total * FPS)
    only = os.environ.get("ONLY_T")  # debug: write single stills
    if only:
        p.kill()
        for ts in only.split(","):
            img, _ = frame(float(ts), SCENES, prev_cache)
            img.save(os.path.join(OUT, f"still-{float(ts):05.1f}.png"))
        return
    for i in range(n):
        t = i / FPS
        idx = scene_index(t, SCENES)
        if idx != last_idx and last_img is not None:
            prev_cache[last_idx] = last_img  # cache before drawing so the slide starts on frame one
        img, idx = frame(t, SCENES, prev_cache)
        last_idx, last_img = idx, img
        p.stdin.write(img.tobytes())
        if i % (FPS * 10) == 0:
            print(f"  {t:5.1f}s", flush=True)
    p.stdin.close()
    p.wait()
    prev = os.path.join(OUT, "k1-bridge-demo-preview.mp4")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", mp4, "-vf", "scale=720:720", "-c:v", "libx264", "-crf", "27",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", prev], check=True)
    print("wrote", mp4, prev)


if __name__ == "__main__":
    main()
