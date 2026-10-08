"""Transcribe the narration with Gemini and diff it against the script text."""
import base64, difflib, re, sys
sys.path.insert(0, "/opt/ai-digest")
sys.path.insert(0, "/opt/opentax-test/video")
import requests
from trends import ENV
import k1video

audio = base64.b64encode(open("/opt/opentax-test/video/out/narration.wav", "rb").read()).decode()
body = {"contents": [{"parts": [{"inlineData": {"mimeType": "audio/wav", "data": audio}},
                                {"text": "Transcribe this audio verbatim. Output only the words spoken, no timestamps."}]}]}
r = requests.post(f"{ENV['GEMINI_BASE']}/gemini-2.5-flash:generateContent", json=body, timeout=300,
                  headers={"x-goog-api-key": ENV["GEMINI_API_KEY"]})
r.raise_for_status()
heard = r.json()["candidates"][0]["content"]["parts"][0]["text"]
script = " ".join(s for sc in k1video.SCENES for s in sc["segs"])
norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower().replace("-", " ")).split()
a, b = norm(script), norm(heard)
ratio = difflib.SequenceMatcher(None, a, b).ratio()
print(f"words script={len(a)} heard={len(b)} match={ratio:.3f}")
for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
    if op != "equal":
        print(f"  {op}: script[{' '.join(a[i1:i2])}] heard[{' '.join(b[j1:j2])}]")
