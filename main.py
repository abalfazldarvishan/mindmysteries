"""MindMysteries - automatic YouTube Shorts maker (no upload yet).

Steps: Gemini writes the script -> Edge-TTS makes the voice -> Pixabay gives
footage -> FFmpeg builds a 9:16 video with burned-in subtitles.
Result: output/video.mp4 and output/metadata.json
"""
import asyncio
import json
import os
import pathlib
import random
import subprocess
import time

import edge_tts
import requests

ROOT = pathlib.Path(__file__).parent
WORK = ROOT / "work"
OUT = ROOT / "output"
TOPICS_FILE = ROOT / "used_topics.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PIXABAY_KEY = os.environ["PIXABAY_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")

FALLBACK_QUERIES = ["abstract", "clouds", "night city", "ocean", "forest", "lights"]


def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Command failed: " + " ".join(map(str, cmd[:4])))
    return result.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip())


# ---------------------------------------------------------------- 1. script
def load_topics():
    if TOPICS_FILE.exists():
        return json.loads(TOPICS_FILE.read_text())
    return []


def make_script(used):
    avoid = "; ".join(used[-60:]) if used else "none yet"
    prompt = f"""You write scripts for MindMysteries, a YouTube Shorts channel about
the mysteries of the human mind (psychology facts, strange mental phenomena, famous
psychology experiments).

Write ONE new Short script, 35-45 seconds when read aloud (about 95-120 words).

Rules:
- Scene 1 is a strong hook: a curious question or surprising claim in under 12 words.
- Use 6-8 scenes. Each scene is 1-2 short spoken sentences.
- The last scene must make people want to watch again or follow (a twist or question).
- Only state well-established, widely documented psychology facts. Do NOT invent
  statistics, study names, or quotes. If unsure about something, leave it out.
- No medical advice, no diagnosing, no claims that something cures or treats anything.
- Simple, conversational English.
- "visual" is 1-3 plain English words describing stock footage that fits the scene
  (concrete things such as "city night", "brain", "person walking", "ocean waves").
- Do not reuse these topics: {avoid}

Return ONLY JSON in this exact shape:
{{
  "topic": "short topic name",
  "title": "catchy title under 70 characters, no hashtags",
  "scenes": [{{"text": "spoken sentence(s)", "visual": "stock footage keywords"}}],
  "description": "2 sentence description for YouTube",
  "tags": ["psychology", "..."]
}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    last_error = None
    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=90,
                              headers={"x-goog-api-key": GEMINI_KEY})
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            assert len(data["scenes"]) >= 4
            for s in data["scenes"]:
                assert s["text"].strip() and s["visual"].strip()
            return data
        except Exception as e:  # retry on any API/format problem
            last_error = e
            print(f"Gemini attempt {attempt + 1} failed: {e}")
            time.sleep(8 * (attempt + 1))
    raise RuntimeError(f"Gemini failed: {last_error}")


# ----------------------------------------------------------------- 2. voice
async def _tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate="+5%").save(str(path))


def make_voice(scenes):
    """One wav per scene (with a short pause), returns list of durations."""
    durations = []
    for i, scene in enumerate(scenes):
        mp3 = WORK / f"voice_{i}.mp3"
        wav = WORK / f"voice_{i}.wav"
        asyncio.run(_tts(scene["text"], mp3))
        run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "44100", "-ac", "2",
             "-af", "apad=pad_dur=0.25", str(wav)])
        durations.append(duration(wav))
    listing = "".join(f"file 'voice_{i}.wav'\n" for i in range(len(scenes)))
    (WORK / "voice_list.txt").write_text(listing)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "voice_list.txt",
         "-c", "copy", "narration.wav"], cwd=WORK)
    return durations


# --------------------------------------------------------------- 3. footage
def find_clip_url(query, used_ids):
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": PIXABAY_KEY, "q": query, "per_page": 12,
                             "safesearch": "true"}, timeout=40)
    r.raise_for_status()
    hits = [h for h in r.json().get("hits", []) if h["id"] not in used_ids]
    random.shuffle(hits)
    for hit in hits:
        videos = hit["videos"]
        for size in ("medium", "small", "large", "tiny"):
            if videos.get(size, {}).get("url"):
                return hit["id"], videos[size]["url"]
    return None, None


def get_footage(scenes, durations):
    used_ids = set()
    scene_files = []
    for i, (scene, dur) in enumerate(zip(scenes, durations)):
        queries = [scene["visual"]] + FALLBACK_QUERIES
        clip_id, url = None, None
        for q in queries:
            clip_id, url = find_clip_url(q, used_ids)
            if url:
                break
        if not url:
            raise RuntimeError("No footage found for scene " + str(i))
        used_ids.add(clip_id)
        raw = WORK / f"raw_{i}.mp4"
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    f.write(chunk)
        out = WORK / f"scene_{i}.mp4"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(raw), "-t", f"{dur:.3f}",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,fps=30,setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", str(out)])
        scene_files.append(out.name)
        print(f"scene {i}: '{scene['visual']}' ok")
    listing = "".join(f"file '{name}'\n" for name in scene_files)
    (WORK / "scene_list.txt").write_text(listing)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "scene_list.txt",
         "-c", "copy", "silent.mp4"], cwd=WORK)


# ------------------------------------------------------------- 4. subtitles
def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def make_subtitles(scenes, durations):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    start = 0.0
    for scene, dur in zip(scenes, durations):
        words = scene["text"].replace("{", "").replace("}", "").split()
        chunks = [" ".join(words[j:j + 3]) for j in range(0, len(words), 3)]
        total_chars = sum(len(c) for c in chunks) or 1
        speech = max(dur - 0.25, 0.5)
        t = start
        for c in chunks:
            part = speech * len(c) / total_chars
            lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + part)},Default,,0,0,0,,{c.upper()}")
            t += part
        start += dur
    (WORK / "subs.ass").write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- 5. final
def build_final():
    run(["ffmpeg", "-y", "-i", "silent.mp4", "-i", "narration.wav",
         "-vf", "ass=subs.ass", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart",
         str(OUT / "video.mp4")], cwd=WORK)


def main():
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    used = load_topics()

    print("1/5 writing script...")
    data = make_script(used)
    scenes = data["scenes"]
    print("Topic:", data["topic"], "| Title:", data["title"])

    print("2/5 making voice...")
    durations = make_voice(scenes)

    print("3/5 getting footage...")
    get_footage(scenes, durations)

    print("4/5 making subtitles...")
    make_subtitles(scenes, durations)

    print("5/5 building final video...")
    build_final()

    tags = [str(t) for t in data.get("tags", [])][:15]
    hashtags = " ".join("#" + t.replace(" ", "") for t in tags[:3])
    metadata = {
        "title": data["title"][:95],
        "description": f"{data['description']}\n\n{hashtags} #Shorts #psychology",
        "tags": tags + ["Shorts"],
        "topic": data["topic"],
    }
    (OUT / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False))

    used.append(data["topic"])
    TOPICS_FILE.write_text(json.dumps(used, indent=2, ensure_ascii=False))
    print("Done:", OUT / "video.mp4")


if __name__ == "__main__":
    main()
