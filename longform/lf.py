"""Long-form (16:9) GTA video builder.

Run with the AI venv:  .venv-ai\\Scripts\\python.exe longform\\lf.py <command> <slug>

A project is data/longform/<slug>/script.json (written by Claude, see the gta-long-form skill):
{
  "title": "...", "voice": "am_michael", "speed": 1.0, "captions": false,
  "thumbnail": {"prompt": "...", "text": "BIG TEXT", "accent": "SMALL TEXT"},
  "segments": [
    {"chapter": "optional chapter title", "text": "narration",
     "lower": "optional lower-third", "card": "optional big on-screen text",
     "shots": [{"type": "gameplay", "tags": ["driving"]}, {"type": "stock", "query": "miami beach aerial"},
               {"type": "ai", "prompt": "..."}]}
  ]
}
Commands: voices, check, voice, images, fetch, render, all.
"""
from __future__ import annotations

import json, math, os, random, re, shutil, subprocess, sys, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "longform"
LIB = DATA / "library"
FPS = 30
SR = 24000
GAP = 0.35          # pause between segments
CHAPTER_GAP = 1.4   # extra breathing room before a chapter card
INTRO_GAP = 6.5     # silence before a segment with INTRO: (route map + title sting plays there)
MAX_CUT = 6.0       # longest single video cut before switching windows
GRADE = ("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,"
         "eq=contrast=1.07:saturation=1.12:brightness=-0.01,"
         "colorbalance=rs=0.03:bs=-0.03:rh=-0.02:bh=0.04,format=yuv420p")
STYLE = ("cinematic film still, Vice City inspired, Miami, golden hour or neon night, anamorphic lens, "
         "35mm film grain, dramatic lighting, highly detailed, photorealistic")
NEGATIVE = ("text, watermark, logo, signature, caption, letters, blurry, lowres, deformed, extra fingers, "
            "cartoon, anime, oversaturated, jpeg artifacts")
IMAGE_MODEL = "SG161222/RealVisXL_V5.0_Lightning"


def ffmpeg() -> str:
    return os.getenv("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_duration(path: Path) -> float:
    probe = shutil.which("ffprobe") or str(Path(ffmpeg()).with_name("ffprobe.exe"))
    out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def load(slug: str) -> tuple[Path, dict]:
    d = DATA / slug
    return d, json.loads((d / "script.json").read_text(encoding="utf-8"))


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def env(name: str) -> str | None:
    if os.getenv(name):
        return os.getenv(name)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


# ---------------------------------------------------------------- check
def _parse(path: Path) -> tuple[dict, list]:
    """script.txt -> (header meta, segments). Header lines TITLE/DESC/THUMB/THUMBTEXT/THUMBACCENT, then blocks
    split by '---' with TEXT:, CH:, LOWER:, CARD:, MAP:, VS:, INTRO: and shot lines 'G tags', 'S query',
    'A prompt', 'P name' (still assets/name.*, shown framed) and 'C name' (clip assets/videos/name.mp4)."""
    head, *blocks = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n---\n")
    meta = dict(line.split(":", 1) for line in head.strip().splitlines() if ":" in line)
    segs = []
    for block in blocks:
        seg: dict = {"shots": []}
        for line in block.strip().splitlines():
            key, _, val = line.partition(":")
            if key in ("TEXT", "CH", "LOWER", "CARD", "MAP", "VS", "INTRO"):
                seg[{"TEXT": "text", "CH": "chapter", "LOWER": "lower", "CARD": "card", "MAP": "map", "VS": "vs",
                     "INTRO": "intro"}[key]] = val.strip()
            elif line[:2] in ("P ", "C "):
                seg["shots"].append({"type": "photo" if line[0] == "P" else "clip", "name": line[2:].strip()})
            elif line[:2] in ("G ", "S ", "A "):
                kind, rest = line[0], line[2:].strip()
                shot = ({"type": "gameplay", "tags": rest.split()} if kind == "G" else
                        {"type": "stock", "query": rest} if kind == "S" else {"type": "ai", "prompt": rest})
                while kind == "A" and (m := re.search(r"\s*#(seed|photo)=(\S+)$", shot["prompt"])):
                    # "#seed=7" redraws one image; "#photo=arch_x" uses assets/arch_x.* (a real photo) when it exists
                    shot["prompt"] = shot["prompt"][:m.start()]
                    shot[m.group(1)] = int(m.group(2)) if m.group(1) == "seed" else m.group(2)
                seg["shots"].append(shot)
        segs.append(seg)
    return meta, segs


def cmd_build(slug: str) -> None:
    """script.txt (easy to write) -> script.json (see _parse for the format)."""
    d = DATA / slug
    meta, segs = _parse(d / "script.txt")
    old = json.loads((d / "script.json").read_text(encoding="utf-8")) if (d / "script.json").exists() else {}
    s = {k: v for k, v in old.items() if k not in ("segments",)}
    s.update({"title": meta.get("TITLE", "").strip(), "description": meta.get("DESC", "").strip(), "segments": segs,
              "thumbnail": {"prompt": meta.get("THUMB", "").strip(), "text": meta.get("THUMBTEXT", "").strip(),
                            "accent": meta.get("THUMBACCENT", "").strip()}})
    save_json(d / "script.json", s)
    print(f"{len(segs)} segments, {sum(len(x['text']) for x in segs)} characters")


def cmd_check(slug: str) -> None:
    d, s = load(slug)
    _check(s["segments"], d / "assets")


def _asset(assets: Path, name: str, videos: bool = False) -> Path | None:
    exts = (".mp4", ".mov", ".webm") if videos else (".jpg", ".jpeg", ".png", ".webp")
    folder = assets / "videos" if videos else assets
    return next((p for p in sorted(folder.glob(name + ".*")) if p.suffix.lower() in exts), None) if folder.exists() else None


def _asset_problems(segs: list, assets: Path) -> list[str]:
    """Every P/C/#photo/VS cue must point at a file in assets/ (VS needs both NN_game and NN_real)."""
    out = []
    for i, seg in enumerate(segs):
        for sh in seg.get("shots", []):
            if sh["type"] in ("photo", "clip") and not _asset(assets, sh["name"], sh["type"] == "clip"):
                out.append(f"segment {i}: {'clip videos/' if sh['type'] == 'clip' else 'photo '}{sh['name']} not in {assets}")
            if sh.get("photo") and not _asset(assets, sh["photo"]):
                out.append(f"segment {i}: #photo={sh['photo']} missing, the AI image will be used")
        if seg.get("vs"):
            num = seg["vs"].split("|")[0].strip()
            for k in ("game", "real"):
                if not _asset(assets, f"{num}_{k}"):
                    out.append(f"segment {i}: VS {num} needs {num}_{k}.*")
    return out


def cmd_lint(slug: str, file: str, assets: str | None = None) -> None:
    """Check a draft script file without touching script.json; asset cues are checked against `assets`
    (default: the video's assets folder)."""
    d = DATA / slug
    _, segs = _parse(Path(file) if Path(file).is_absolute() else d / file)
    _check(segs, Path(assets) if assets else d / "assets")


def _check(segs: list, assets: Path) -> None:
    words = sum(len(x["text"].split()) for x in segs)
    shots = sum(len(x.get("shots", [])) for x in segs)
    kinds: dict[str, int] = {}
    problems = []
    for i, seg in enumerate(segs):
        n = len(seg["text"].split())
        if not seg.get("shots"):
            problems.append(f"segment {i}: no shots")
        elif n / len(seg["shots"]) > 22:
            problems.append(f"segment {i}: {n} words over {len(seg['shots'])} shots (aim for one shot per ~13 words)")
        for sh in seg.get("shots", []):
            kinds[sh["type"]] = kinds.get(sh["type"], 0) + 1
    if segs[0].get("chapter"):
        problems.append("segment 0 should be the cold-open hook, without a chapter")
    problems += _asset_problems(segs, assets)
    minutes = words / 155
    print(f"{words} words ~ {minutes:.1f} min, {len(segs)} segments, {shots} shots {kinds}, "
          f"chapters: {sum(1 for x in segs if x.get('chapter'))}")
    for p in problems:
        print("  !", p)


# ---------------------------------------------------------------- voice
VOICE_SAMPLE = ("Vice City was never just a setting. It was a promise. And for twelve years, Rockstar kept "
                "everyone waiting to find out if they could keep it. But hidden in the very first trailer was a "
                "detail almost nobody noticed. A single license plate. And once you see what's written on it, "
                "you can't unsee it.")


def _pipeline(voice: str):
    from kokoro import KPipeline
    return KPipeline(lang_code="b" if voice.startswith("b") else "a", repo_id="hexgrad/Kokoro-82M")


def _speak(pipe, text: str, voice: str, speed: float):
    import numpy as np
    audio, words, t = [], [], 0.0
    for r in pipe(text, voice=voice, speed=speed, split_pattern=r"\n+"):
        a = r.audio.cpu().numpy() if hasattr(r.audio, "cpu") else np.asarray(r.audio)
        for tok in r.tokens or []:
            if tok.start_ts is not None and tok.text.strip() and re.search(r"\w", tok.text):
                words.append({"w": tok.text, "s": round(t + tok.start_ts, 3), "e": round(t + (tok.end_ts or tok.start_ts), 3)})
        audio.append(a)
        t += len(a) / SR
    return (np.concatenate(audio) if audio else np.zeros(1)), words


def write_wav(path: Path, samples) -> None:
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())


def cmd_voices(_slug: str | None = None) -> None:
    out = DATA / "voice-samples"
    out.mkdir(parents=True, exist_ok=True)
    for v in ["am_michael", "am_onyx", "bm_george"]:
        audio, _ = _speak(_pipeline(v), VOICE_SAMPLE, v, 1.0)
        write_wav(out / f"kokoro_{v}.wav", audio)
    ref = next((p for p in out.glob("ref*.wav")), None)  # a voice sample you may use, e.g. ref_me.wav
    for name, ex, cfg in [("calm", 0.5, 0.5), ("dramatic", 0.7, 0.35)]:
        job = {"items": [{"text": t, "out": str(out / f"_cb_{name}_{n}.wav")} for n, t in enumerate(_sentences(VOICE_SAMPLE))],
               "exaggeration": ex, "cfg_weight": cfg, "temperature": 0.75}
        if ref:
            job["ref"] = str(ref)
        save_json(out / "job.json", job)
        subprocess.run([str(VOICE_PY), str(Path(__file__).with_name("cb_voice.py")), str(out / "job.json")], check=True)
        import numpy as np, soundfile as sf
        parts = []
        for item in job["items"]:
            a, _sr = sf.read(item["out"], dtype="float32")
            parts += [a, np.zeros(int(SENTENCE_GAP * SR), dtype="float32")]
            Path(item["out"]).unlink()
        write_wav(out / f"chatterbox_{name}{'_ref' if ref else ''}.wav", np.concatenate(parts))
    print("samples in", out)


VOICE_PY = ROOT / ".venv-voice" / "Scripts" / "python.exe"
SENTENCE_GAP = 0.2


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", text.strip())
    out: list[str] = []
    for p in parts:  # glue very short sentences to the previous one; Chatterbox is shaky on 1-3 words
        if out and len(p.split()) < 4:
            out[-1] += " " + p
        else:
            out.append(p)
    return out


_WHISPER = None


def _whisper():
    global _WHISPER
    if _WHISPER is None:
        import torch  # its bundled CUDA libraries (cuBLAS, cuDNN) let faster-whisper use the GPU
        lib = Path(torch.__file__).parent / "lib"
        os.environ["PATH"] = str(lib) + os.pathsep + os.environ["PATH"]
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(lib))
        from faster_whisper import WhisperModel
        try:
            _WHISPER = WhisperModel("small.en", device="cuda", compute_type="float16")
        except Exception:
            _WHISPER = WhisperModel("small.en", device="cpu", compute_type="int8")
    return _WHISPER


def _norm(text: str) -> list[str]:
    """Words for comparing script vs Whisper: digits become spoken words (Whisper writes 1912, the script
    says nineteen twelve), apostrophes and hyphens dropped."""
    from num2words import num2words

    def spoken(m: re.Match) -> str:
        raw = m.group(0).replace(",", "").replace(" ", "")
        if "." in raw:
            a, b = raw.split(".", 1)
            return f"{num2words(int(a)) if a else ''} point {' '.join(num2words(int(c)) for c in b)}"
        n = int(raw)
        if 1100 <= n <= 2099 and "," not in m.group(0) and not 2000 <= n <= 2009:  # years: nineteen twelve
            hi, lo = divmod(n, 100)
            return f"{num2words(hi)} {num2words(lo) if lo >= 10 else ('oh ' + num2words(lo)) if lo else 'hundred'}"
        return num2words(n)
    text = re.sub(r"\d(?:\d|\s?,\d)*(?:\s?\.\d+)?", spoken, text.lower())
    return re.findall(r"[a-z]+", text.replace("'", "").replace("-", " "))


def _transcribe(path: Path):
    import numpy as np, soundfile as sf
    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if sr != 16000:  # faster-whisper wants 16 kHz mono
        a = np.interp(np.arange(0, len(a), sr / 16000), np.arange(len(a)), a).astype("float32")
    segs, _ = _whisper().transcribe(a, language="en", word_timestamps=True, beam_size=5)
    words = [{"w": w.word.strip(), "s": w.start, "e": w.end} for sg in segs for w in (sg.words or [])]
    return words


def _chatterbox_segments(d: Path, s: dict) -> list[tuple]:
    """Per segment: (audio, words). Each sentence is spoken separately, checked with Whisper,
    and re-spoken with a new seed (up to 3 tries) when the words don't match the script."""
    import difflib
    import numpy as np, soundfile as sf
    work = d / "tts"; work.mkdir(exist_ok=True)
    sents = [(i, k, t) for i, seg in enumerate(s["segments"]) for k, t in enumerate(_sentences(seg["text"]))]
    seeds = {(i, k): 0 for i, k, _ in sents}
    good: dict[tuple, list] = {}
    for attempt in range(3):
        todo = [(i, k, t) for i, k, t in sents if (i, k) not in good and not (work / f"{i:03d}_{k}.wav").exists()]
        if todo:
            job = {"items": [{"text": t, "out": str(work / f"{i:03d}_{k}.wav"), "seed": seeds[(i, k)]} for i, k, t in todo],
                   "exaggeration": s.get("exaggeration", 0.55), "cfg_weight": s.get("cfg_weight", 0.45),
                   "temperature": s.get("temperature", 0.75)}
            if s.get("voice_ref"):
                job["ref"] = str((d / s["voice_ref"]) if not Path(s["voice_ref"]).is_absolute() else s["voice_ref"])
            save_json(work / "job.json", job)
            subprocess.run([str(VOICE_PY), str(Path(__file__).with_name("cb_voice.py")), str(work / "job.json")], check=True)
        for i, k, t in sents:
            if (i, k) in good:
                continue
            words = _transcribe(work / f"{i:03d}_{k}.wav")
            score = difflib.SequenceMatcher(None, "".join(_norm(t)), "".join(_norm(" ".join(w["w"] for w in words)))).ratio()
            if score >= 0.9 or attempt == 2:
                good[(i, k)] = words
                if score < 0.9:
                    print(f"  ! segment {i} sentence {k} still differs after 3 tries ({score:.2f}): listen to it")
            else:
                print(f"  retry segment {i} sentence {k} (match {score:.2f})")
                (work / f"{i:03d}_{k}.wav").unlink()
                seeds[(i, k)] += 1
    result = []
    for i, seg in enumerate(s["segments"]):
        audio, words, t = [], [], 0.0
        for k, _ in enumerate(_sentences(seg["text"])):
            a, sr = sf.read(work / f"{i:03d}_{k}.wav", dtype="float32")
            if a.ndim > 1:
                a = a.mean(axis=1)
            if sr != SR:
                import torch, torchaudio
                a = torchaudio.functional.resample(torch.from_numpy(a), sr, SR).numpy()
            ws = good[(i, k)]
            end = min(len(a) / SR, (ws[-1]["e"] + 0.25) if ws else len(a) / SR)  # cut trailing noise
            start = max(0.0, (ws[0]["s"] - 0.08) if ws else 0.0)
            a = a[int(start * SR):int(end * SR)]
            fade = min(len(a), int(0.02 * SR))
            if fade:
                a[-fade:] *= np.linspace(1, 0, fade)
            words += [{"w": w["w"], "s": round(t + w["s"] - start, 3), "e": round(t + w["e"] - start, 3)} for w in ws]
            audio += [a, np.zeros(int(SENTENCE_GAP * SR), dtype="float32")]
            t += len(a) / SR + SENTENCE_GAP
        result.append((np.concatenate(audio[:-1]), words))
    return result


EL_API = "https://api.elevenlabs.io/v1"


def _el_key() -> str:
    key = env("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("Add ELEVENLABS_API_KEY=... to .env (ElevenLabs > Developers > API keys)")
    return key


def cmd_el_voices(_slug=None) -> None:
    """Lists the voices in your ElevenLabs account (add library voices to 'My voices' first)."""
    import requests
    r = requests.get(f"{EL_API}/voices", headers={"xi-api-key": _el_key()}, timeout=30)
    r.raise_for_status()
    for v in r.json()["voices"]:
        labels = ", ".join(f"{v2}" for v2 in (v.get("labels") or {}).values())
        print(f"{v['voice_id']}  {v['name']}  [{v.get('category')}] {labels}")
    u = requests.get(f"{EL_API}/user/subscription", headers={"xi-api-key": _el_key()}, timeout=30)
    if u.ok:
        j = u.json()
        print(f"\nplan {j.get('tier')}: {j.get('character_count')}/{j.get('character_limit')} characters used this period")


def cmd_el_find(query: str) -> None:
    """Searches the ElevenLabs Voice Library (English narration voices) and saves each preview MP3."""
    import requests
    r = requests.get(f"{EL_API}/shared-voices", headers={"xi-api-key": _el_key()}, timeout=30,
                     params={"search": query, "language": "en", "page_size": 12, "sort": "trending"})
    r.raise_for_status()
    out = DATA / "voice-samples" / "elevenlabs"; out.mkdir(parents=True, exist_ok=True)
    for v in r.json().get("voices", []):
        name = re.sub(r"[^A-Za-z0-9]+", "_", v["name"])[:40]
        print(f"{v['voice_id']}  {v['name']}  ({v.get('gender')}, {v.get('accent')}, {v.get('age')}) "
              f"used {v.get('usage_character_count_1y', 0):,} chars/yr  public_owner_id={v.get('public_owner_id')}")
        if v.get("preview_url"):
            (out / f"{name}.mp3").write_bytes(requests.get(v["preview_url"], timeout=60).content)
    print("previews in", out)


def cmd_el_add(arg: str) -> None:
    """Adds a library voice to My Voices: lf.py el-add <public_owner_id>:<voice_id>"""
    import requests
    owner, vid = arg.split(":")
    r = requests.post(f"{EL_API}/voices/add/{owner}/{vid}", headers={"xi-api-key": _el_key()},
                      json={"new_name": f"lib_{vid[:6]}"}, timeout=30)
    print(r.status_code, r.text[:200])


def _elevenlabs_segments(d: Path, s: dict) -> list[tuple]:
    """One request per segment with the neighbours' text for consistent delivery. Cached in tts/ so
    a rerun never spends credits twice; delete tts/el_NNN.json to redo a segment."""
    import base64, requests
    import numpy as np
    work = d / "tts"; work.mkdir(exist_ok=True)
    voice_id = s.get("el_voice", "cBTYF7NiJbR8Bvmdeuse") or raise_(SystemExit('Set "el_voice": "<voice id>" in script.json (lf.py el-voices lists them)'))
    texts = [seg["text"] for seg in s["segments"]]
    out = []
    for i, text in enumerate(texts):
        import hashlib
        settings = [voice_id, s.get("el_model", "eleven_multilingual_v2"), s.get("stability", 0.45), s.get("style", 0.15),
                    s.get("speed", 1.0)]
        cache = work / f"el_{hashlib.sha1(json.dumps([text] + settings).encode()).hexdigest()[:12]}.json"
        legacy = work / f"el_{i:03d}.json"  # older index-named cache, adopted once
        if not cache.exists() and legacy.exists():
            legacy.rename(cache)
        if not cache.exists():
            body = {"text": text, "model_id": s.get("el_model", "eleven_multilingual_v2"),
                    "voice_settings": {"stability": s.get("stability", 0.45), "similarity_boost": 0.8,
                                       "style": s.get("style", 0.15), "use_speaker_boost": True,
                                       "speed": s.get("speed", 1.0)}}
            if i:
                body["previous_text"] = texts[i - 1]
            if i + 1 < len(texts):
                body["next_text"] = texts[i + 1]
            r = requests.post(f"{EL_API}/text-to-speech/{voice_id}/with-timestamps?output_format=pcm_24000",
                              headers={"xi-api-key": _el_key()}, json=body, timeout=300)
            if not r.ok:
                raise SystemExit(f"ElevenLabs error on segment {i}: {r.status_code} {r.text[:300]}")
            cache.write_text(r.text, encoding="utf-8")
            print(f"segment {i}: {len(text)} characters")
        j = json.loads(cache.read_text(encoding="utf-8"))
        audio = np.frombuffer(base64.b64decode(j["audio_base64"]), dtype="<i2").astype("float32") / 32768
        al = j.get("alignment") or j.get("normalized_alignment") or {}
        words, cur, start, end = [], "", None, 0.0
        for ch, a, b in zip(al.get("characters", []), al.get("character_start_times_seconds", []),
                            al.get("character_end_times_seconds", [])):
            if ch.isspace():
                if cur.strip() and re.search(r"\w", cur):
                    words.append({"w": cur, "s": round(start, 3), "e": round(end, 3)})
                cur, start = "", None
            else:
                cur += ch; start = a if start is None else start; end = b
        if cur.strip() and re.search(r"\w", cur):
            words.append({"w": cur, "s": round(start, 3), "e": round(end, 3)})
        out.append((audio, words))
    return out


def raise_(e):
    raise e


def cmd_voice(slug: str) -> None:
    import numpy as np
    d, s = load(slug)
    engine = s.get("engine", "elevenlabs")
    voice, speed = s.get("voice", "am_michael"), float(s.get("speed", 1.0))
    if engine == "elevenlabs":
        spoken = _elevenlabs_segments(d, s)
    elif engine == "chatterbox":
        spoken = _chatterbox_segments(d, s)
    else:
        pipe = _pipeline(voice)
        spoken = [_speak(pipe, seg["text"], voice, speed) for seg in s["segments"]]
    parts, timeline, t = [], [], 0.0
    for i, seg in enumerate(s["segments"]):
        lead = INTRO_GAP if seg.get("intro") else CHAPTER_GAP if seg.get("chapter") and i else 0.0
        if lead:
            parts.append(np.zeros(int(lead * SR))); t += lead
        audio, words = spoken[i]
        start = t
        parts.append(audio); t += len(audio) / SR
        parts.append(np.zeros(int(GAP * SR))); t += GAP
        timeline.append({"start": round(start - lead, 3), "speech": round(start, 3), "end": round(t, 3),
                         "words": [{**w, "s": round(w["s"] + start, 3), "e": round(w["e"] + start, 3)} for w in words]})
        print(f"segment {i}: {len(audio)/SR:.1f}s")
    write_wav(d / "narration.wav", np.concatenate(parts))
    save_json(d / "voice.json", {"duration": round(t, 3), "segments": timeline})
    _write_srt(d, timeline)
    print(f"narration {t/60:.1f} min -> {d/'narration.wav'}")


def _ts(x: float, sep=",") -> str:
    h, rem = divmod(x, 3600); m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(sec):02d}{sep}{int(round((sec % 1) * 1000)):03d}"


def _caption_groups(words, size=7):
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= size or w["w"][-1:] in ".?!,;:":
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    return [{"text": " ".join(x["w"] for x in g).replace(" ,", ",").replace(" .", "."), "s": g[0]["s"], "e": g[-1]["e"]} for g in groups]


def _write_srt(d: Path, timeline) -> None:
    lines = []
    for n, g in enumerate((g for seg in timeline for g in _caption_groups(seg["words"], 10)), 1):
        lines += [str(n), f"{_ts(g['s'])} --> {_ts(g['e'] + 0.15)}", g["text"], ""]
    (d / "captions.srt").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- AI images
def _image_pipe():
    import torch
    from diffusers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
    pipe = StableDiffusionXLPipeline.from_pretrained(IMAGE_MODEL, torch_dtype=torch.float16, variant="fp16")
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, algorithm_type="sde-dpmsolver++",
                                                             use_karras_sigmas=True)
    pipe.enable_model_cpu_offload()
    pipe.vae.enable_tiling()
    return pipe


def _gen(pipe, prompt: str, path: Path, seed: int, w=1344, h=768) -> None:
    import torch
    img = pipe(prompt=f"{prompt}, {STYLE}", negative_prompt=NEGATIVE, width=w, height=h, num_inference_steps=6,
               guidance_scale=1.6, generator=torch.Generator("cpu").manual_seed(seed)).images[0]
    img = img.resize((1920, 1097), resample=3).crop((0, 8, 1920, 1088))  # Lanczos up to 1080p
    img.save(path, quality=95)


def _adopt_dropped(out: Path) -> None:
    """Images made elsewhere (e.g. ChatGPT) saved as ai/NNN_J.png/.webp/.jpeg become 1920x1080 ai/NNN_J.jpg."""
    from PIL import Image, ImageOps
    for p in list(out.iterdir()) + [out.parent / x for x in ("thumb_bg.png", "thumb_bg.webp", "thumb_bg.jpeg")]:
        if not p.exists() or p.suffix.lower() not in (".png", ".webp", ".jpeg") or "_orig" in p.stem:
            continue
        img = ImageOps.fit(Image.open(p).convert("RGB"), (1920, 1080), method=Image.LANCZOS)
        img.save(p.with_suffix(".jpg"), quality=95)
        p.rename(p.with_name(p.stem + "_orig" + p.suffix))
        print("using dropped image", p.name)


def cmd_prompts(slug: str) -> None:
    """Writes ai-prompts.txt: one ChatGPT-ready prompt per AI shot, with the file name to save it as."""
    d, s = load(slug)
    lines = ["Paste each prompt into ChatGPT, save the image into the ai folder with the given name",
             f"({d / 'ai'}). Any of .png .webp .jpg works; images already there are skipped by `images`.", ""]
    shots = [(f"{i:03d}_{j}", sh["prompt"]) for i, seg in enumerate(s["segments"])
             for j, sh in enumerate(seg["shots"]) if sh["type"] == "ai"]
    if s.get("thumbnail", {}).get("prompt"):
        shots.append(("thumb_bg (save next to script.json, not in ai)", s["thumbnail"]["prompt"]))
    for name, prompt in shots:
        lines += [f"== {name}", f"Create a wide 16:9 image: {prompt}. {STYLE}. No text, logos or watermarks.", ""]
    (d / "ai").mkdir(exist_ok=True)
    (d / "ai-prompts.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(shots)} prompts -> {d / 'ai-prompts.txt'}")


def _photo(d: Path, shot: dict) -> Path | None:
    """The real photo for an 'A ... #photo=name' shot, if the user has put assets/name.* in place."""
    if not shot.get("photo"):
        return None
    return next((p for p in sorted((d / "assets").glob(shot["photo"] + ".*"))
                 if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")), None)


def cmd_images(slug: str) -> None:
    d, s = load(slug)
    out = d / "ai"; out.mkdir(exist_ok=True)
    _adopt_dropped(out)
    todo = [(i, j, sh) for i, seg in enumerate(s["segments"]) for j, sh in enumerate(seg["shots"]) if sh["type"] == "ai"]
    thumb = s.get("thumbnail", {}).get("prompt")
    missing = [x for x in todo if not (out / f"{x[0]:03d}_{x[1]}.jpg").exists() and not _photo(d, x[2])]
    if not missing and (not thumb or (d / "thumb_bg.jpg").exists()):
        print("all AI images exist"); return
    pipe = _image_pipe()
    for n, (i, j, sh) in enumerate(missing, 1):
        _gen(pipe, sh["prompt"], out / f"{i:03d}_{j}.jpg", seed=sh.get("seed", i * 100 + j))
        print(f"image {n}/{len(missing)}")
    if thumb and not (d / "thumb_bg.jpg").exists():
        _gen(pipe, thumb, d / "thumb_bg.jpg", seed=7)


# ---------------------------------------------------------------- stock + gameplay
def _download(url: str, path: Path) -> None:
    import requests
    if path.exists():
        return
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(path.with_suffix(".part"), "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
    path.with_suffix(".part").replace(path)


def _stock(query: str) -> list[Path]:
    """Up to 3 landscape stock videos for a query, from Pixabay (PIXABAY_API_KEY) or Pexels (PEXELS_API_KEY).
    Cached in library/stock; a query with no hits is retried with its last word dropped."""
    import requests
    cache = LIB / "stock"; cache.mkdir(parents=True, exist_ok=True)
    index_file = cache / "index.json"
    index = json.loads(index_file.read_text()) if index_file.exists() else {}
    if query in index and index[query]:
        return [cache / f for f in index[query] if (cache / f).exists()]
    pix, pex = env("PIXABAY_API_KEY"), env("PEXELS_API_KEY")
    if not (pix or pex):
        raise SystemExit("Set PIXABAY_API_KEY in .env (free at https://pixabay.com/api/docs/)")
    words, files = query.split(), []
    while len(words) >= min(2, len(query.split())) and not files:
        q = " ".join(words)
        if pix:
            r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                             params={"key": pix, "q": q[:100], "per_page": 50, "safesearch": "true"})
            r.raise_for_status()
            # Pixabay matches loosely: keep hits whose tags hold half the words, one of them specific.
            qw = [w.lower() for w in words]
            score = lambda h: sum(w in h.get("tags", "").lower() for w in qw)
            generic = {"aerial", "night", "sunset", "fast", "field", "view", "old", "empty", "smoke", "storm"}
            key_hit = lambda h: any(w in h.get("tags", "").lower() for w in qw if w not in generic)
            hits = [h for h in r.json().get("hits", []) if key_hit(h) and score(h) >= max(1, -(-len(qw) // 2))]
            hits.sort(key=lambda h: (-score(h), -h.get("downloads", 0)))
            for v in hits:
                vids = [f for f in v.get("videos", {}).values() if f.get("url") and f.get("width", 0) >= 1280
                        and f["width"] > f.get("height", 0)]
                if not vids:
                    continue
                best = min(vids, key=lambda f: abs(f["width"] - 1920))
                name = f"pixabay_{v['id']}.mp4"
                _download(best["url"], cache / name)
                files.append(name)
                if len(files) == 3:
                    break
        else:
            r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": pex}, timeout=30,
                             params={"query": q, "orientation": "landscape", "per_page": 6})
            r.raise_for_status()
            for v in r.json().get("videos", [])[:3]:
                cands = [f for f in v["video_files"] if f.get("width") and f["width"] >= 1280 and f.get("file_type") == "video/mp4"]
                if cands:
                    best = min(cands, key=lambda f: abs(f["width"] - 1920))
                    name = f"pexels_{v['id']}.mp4"
                    _download(best["link"], cache / name)
                    files.append(name)
        words = words[:-1]
    index[query] = files
    save_json(index_file, index)
    return [cache / f for f in files]


def _gameplay(tags: list[str]) -> list[Path]:
    folder = LIB / "gameplay"
    clips = [p for p in folder.rglob("*") if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm")]
    if not clips:
        raise SystemExit(f"No gameplay yet: put 16:9 GTA gameplay videos in {folder} (name them with tags, "
                         "e.g. driving_police_night.mp4)")
    words = lambda p: set(re.split(r"[^a-z0-9]+", str(p.relative_to(folder)).lower()))
    scored = sorted(clips, key=lambda p: -len(words(p) & {t.lower() for t in tags}))
    best = len(words(scored[0]) & {t.lower() for t in tags})
    return [p for p in scored if len(words(p) & {t.lower() for t in tags}) == best]


class Windows:
    """Hands out non-overlapping windows of source videos, so no moment appears twice."""

    def __init__(self, seed: int):
        self.used: dict[str, list[tuple[float, float]]] = {}
        self.dur: dict[str, float] = {}
        self.rng = random.Random(seed)

    def take(self, sources: list[Path], length: float) -> tuple[Path, float]:
        order = sources[:]; self.rng.shuffle(order)
        order.sort(key=lambda p: sum(b - a for a, b in self.used.get(str(p), [])) / max(self._d(p), 1))
        for p in order:
            dur = self._d(p)
            margin = 3.0 if dur > 30 else 0.3
            for _ in range(40):
                if dur - 2 * margin < length:
                    break
                a = self.rng.uniform(margin, dur - margin - length)
                if all(a + length <= x or a >= y for x, y in self.used.get(str(p), [])):
                    self.used.setdefault(str(p), []).append((a, a + length))
                    return p, a
        p = order[0]  # everything used up: reuse the least-used source
        return p, self.rng.uniform(0, max(0.0, self._d(p) - length))

    def _d(self, p: Path) -> float:
        if str(p) not in self.dur:
            self.dur[str(p)] = ffprobe_duration(p)
        return self.dur[str(p)]


SOURCES = LIB / "gameplay-source"
SHEET_EVERY = 30  # seconds between contact-sheet frames


def cmd_sheets(_slug=None) -> None:
    """Contact sheets (6x5 frames, one every 30 s, timestamped) of every source video, for tagging."""
    out = SOURCES / "sheets"; out.mkdir(parents=True, exist_ok=True)
    font = "C\\:/Windows/Fonts/arialbd.ttf"
    for src in sorted(SOURCES.glob("*.mp4")) + sorted(SOURCES.glob("*.webm")):
        if list(out.glob(f"{src.stem}_*.jpg")):
            continue
        vf = (f"fps=1/{SHEET_EVERY},scale=320:180,drawtext=fontfile='{font}':text='%{{pts\\:hms}}':x=6:y=6:"
              "fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.6,tile=6x5")
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-skip_frame", "nokey", "-i", str(src), "-vf", vf,
                        "-fps_mode", "vfr", "-q:v", "4", str(out / f"{src.stem}_%02d.jpg")], check=True)
        print("sheets for", src.name)


def cmd_tag(_slug=None) -> None:
    """Cuts tagged ranges listed in gameplay-source/tags.txt into library/gameplay/<tags>__<id>_<start>.mp4.
    tags.txt lines: <video id> <start mm:ss> <end mm:ss> tag tag tag"""
    dest = LIB / "gameplay"; dest.mkdir(parents=True, exist_ok=True)
    secs = lambda t: sum(float(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))
    n = 0
    for line in (SOURCES / "tags.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        vid, a, b, *tags = line.split()
        src = next(SOURCES.glob(f"{vid}.*4")) if list(SOURCES.glob(f"{vid}.*4")) else next(SOURCES.glob(f"{vid}.webm"))
        name = f"{'_'.join(tags)}__{vid}_{int(secs(a))}.mp4"
        if (dest / name).exists():
            continue
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", str(secs(a)), "-i", str(src), "-t", str(secs(b) - secs(a)),
                        "-an", "-c", "copy", str(dest / name)], check=True)
        n += 1
    print(f"{n} new clips in {dest}")


def _credits(timeline_dir: Path) -> list[str]:
    """CC BY credit lines for every gameplay source used in this video's shots."""
    used = set()
    for meta in (timeline_dir / "shots").glob("*.src"):
        m = re.search(r"__([A-Za-z0-9_-]{11})_\d+\.mp4", meta.read_text(encoding="utf-8"))
        if m:
            used.add(m.group(1))
    lines = []
    for vid in sorted(used):
        info = SOURCES / f"{vid}.info.json"
        if info.exists():
            j = json.loads(info.read_text(encoding="utf-8"))
            lines.append(f"- {j.get('channel')}: \"{j.get('title')}\" https://www.youtube.com/watch?v={vid} (CC BY)")
    return lines


def _cut(src: Path, start: float, length: float, out: Path) -> None:
    subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
                    "-vf", GRADE, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out)], check=True)


def cmd_fetch(slug: str) -> None:
    d, s = load(slug)
    voice = json.loads((d / "voice.json").read_text(encoding="utf-8"))
    shots_dir = d / "shots"; shots_dir.mkdir(exist_ok=True)
    win = Windows(seed=len(slug))
    timeline, motions = [], ["in", "left", "out", "right"]
    for i, (seg, tv) in enumerate(zip(s["segments"], voice["segments"])):
        start, end = tv["start"], tv["end"]
        if i + 1 < len(voice["segments"]):
            end = voice["segments"][i + 1]["start"]
        else:
            end += 1.5  # tail for the outro fade
        shots = seg["shots"]
        each = (end - start) / len(shots)
        t = start
        for j, sh in enumerate(shots):
            if sh["type"] == "photo":  # a real photo: shown whole on a blurred copy of itself, slow push in
                p = _asset(d / "assets", sh["name"])
                if not p:
                    raise SystemExit(f"segment {i} shot {j}: assets/{sh['name']}.* missing (run lint)")
                from PIL import Image
                with Image.open(p) as im:  # a 16:9 frame of 1600 px+ (e.g. a game screenshot) fills the screen
                    full = im.width >= 1600 and abs(im.width / im.height - 16 / 9) < 0.08
                timeline.append({"kind": "image", "src": f"assets/{p.name}", "start": round(t, 3), "dur": round(each, 3),
                                 "motion": motions[(i + j) % 4] if full else "in", **({} if full else {"fit": "frame"}),
                                 "credit": "photo"})
                t += each
                continue
            if sh["type"] == "clip":  # a real video clip from assets/videos, slowed (max 1.6x) to fill its slot
                p = _asset(d / "assets", sh["name"], videos=True)
                if not p:
                    raise SystemExit(f"segment {i} shot {j}: assets/videos/{sh['name']}.* missing (run lint)")
                name = f"{i:03d}_{j}_clip.mp4"
                if not (shots_dir / name).exists():
                    stretch = min(1.6, max(1.0, each / max(0.1, ffprobe_duration(p) - 0.1)))
                    subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", str(p), "-t", f"{each + 0.2:.3f}", "-vf",
                                    f"setpts={stretch:.3f}*PTS,tpad=stop_mode=clone:stop_duration=10,scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(shots_dir / name)], check=True)
                timeline.append({"kind": "video", "src": f"shots/{name}", "start": round(t, 3), "dur": round(each, 3),
                                 "credit": "clip"})
                t += each
                continue
            if sh["type"] == "ai":
                photo = _photo(d, sh)
                timeline.append({"kind": "image", "src": f"assets/{photo.name}" if photo else f"ai/{i:03d}_{j}.jpg", "start": round(t, 3), "dur": round(each, 3),
                                 "motion": motions[(i + j) % 4], **({"fit": "frame"} if photo else {})})
            else:
                sources = (_stock(sh["query"]) or _gameplay(sh["query"].split())) if sh["type"] == "stock" else _gameplay(sh.get("tags", []))
                if not sources:
                    raise SystemExit(f"segment {i} shot {j}: nothing found for {sh}")
                n = max(1, math.ceil(each / MAX_CUT))
                for k in range(n):
                    length = each / n
                    name = f"{i:03d}_{j}_{k}.mp4"
                    meta = shots_dir / (name + ".src")
                    if not (shots_dir / name).exists():
                        src, a = win.take(sources, length + 0.2)
                        _cut(src, a, length + 0.2, shots_dir / name)
                        meta.write_text(f"{src.name} {a:.1f}", encoding="utf-8")
                    timeline.append({"kind": "video", "src": f"shots/{name}", "start": round(t + k * length, 3),
                                     "dur": round(length, 3), "credit": sh["type"]})
            t += each
        print(f"segment {i}: {len(shots)} shots")
    save_json(d / "timeline.json", timeline)


# ---------------------------------------------------------------- render
def _whoosh(path: Path) -> None:
    if path.exists():
        return
    subprocess.run([ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=0.9:amplitude=0.6",
                    "-af", "highpass=f=300,lowpass=f=3500,afade=t=in:d=0.45:curve=exp,afade=t=out:st=0.45:d=0.45,volume=0.8",
                    "-ar", "48000", str(path)], check=True)


def cmd_render(slug: str, preview: bool = False) -> None:
    d, s = load(slug)
    voice = json.loads((d / "voice.json").read_text(encoding="utf-8"))
    timeline = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
    _whoosh(d / "whoosh.wav")
    music = sorted(p for p in (LIB / "music").glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a")) if (LIB / "music").exists() else []
    tracks = []
    if music:
        rng = random.Random(slug)
        rng.shuffle(music)
        (d / "music").mkdir(exist_ok=True)
        t = 0.0
        for m in music * 20:
            if t >= voice["duration"]:
                break
            shutil.copy(m, d / "music" / m.name)
            length = ffprobe_duration(m)
            tracks.append({"src": f"music/{m.name}", "start": round(t, 3), "dur": round(length, 3)})
            t += length - 2
    chapters, lowers, cards, captions, maps, compares, intro = [], [], [], [], [], [], None
    (d / "assets").mkdir(exist_ok=True)
    n = 0
    for seg, tv in zip(s["segments"], voice["segments"]):
        if seg.get("chapter"):
            n += 1
            chapters.append({"title": seg["chapter"], "number": n, "start": tv["start"]})
        if seg.get("lower"):
            lowers.append({"text": seg["lower"], "start": tv["speech"] + 0.3, "dur": 4.5})
        if seg.get("map"):  # MAP: Name | real place | lon, lat
            name, place, ll = [x.strip() for x in seg["map"].split("|")]
            lon, lat = [float(x) for x in ll.split(",")]
            maps.append({"name": name, "place": place, "lon": lon, "lat": lat,
                         "start": tv["start"] if seg.get("chapter") else tv["speech"]})
        if seg.get("intro"):  # INTRO: title | sub  -> route map + title in the silence before this segment
            title, _, sub = [x.strip() for x in seg["intro"].partition("|")]
            intro = {"start": round(tv["start"] - INTRO_GAP, 3), "dur": INTRO_GAP, "title": title, "sub": sub}
        if seg.get("vs"):  # VS: NN [| name | place] -> assets/NN_game.* next to assets/NN_real.*
            vs, focus = seg["vs"], None   # "#focus=20" shows that horizontal % of the game shot in its half-panel
            if m := re.search(r"\s*#focus=(\d+)\s*$", vs):
                vs, focus = vs[:m.start()], int(m.group(1))
            num, *label = [x.strip() for x in vs.split("|")]   # labels default to the latest MAP point
            pair = [next(iter(sorted((d / "assets").glob(f"{num}_{k}.*"))), None) for k in ("game", "real")]
            if all(pair) and (maps or len(label) == 2):
                name, place = label if len(label) == 2 else (maps[-1]["name"], maps[-1]["place"])
                at = tv["speech"] + 0.2
                if seg.get("chapter"):   # wait for the chapter card (2.6 s from the segment start) to clear
                    at = max(at, tv["start"] + 2.7)
                compares.append({"game": f"assets/{pair[0].name}", "real": f"assets/{pair[1].name}", "name": name,
                                 "place": place, "start": round(at, 3), "dur": round(min(5.0, tv["end"] - at), 3),
                                 **({"focus": focus} if focus is not None else {})})
            else:
                print(f"VS {num}: missing assets/{num}_game.* or _real.*, skipped")
        if seg.get("card"):
            cards.append({"text": seg["card"], "start": tv["speech"], "dur": min(4.0, tv["end"] - tv["speech"])})
        if s.get("captions"):
            captions += _caption_groups(tv["words"])
    duration = voice["duration"] + 1.5
    if preview:
        duration = min(duration, float(preview))
    props = {"durationSeconds": round(duration, 3), "narration": "narration.wav", "music": tracks,
             "musicVolume": s.get("musicVolume", 0.10), "shots": timeline, "chapters": chapters, "lowers": lowers,
             "cards": cards, "captions": captions, "maps": maps, "compares": compares, "intro": intro, "whoosh": "whoosh.wav", "title": s["title"]}
    save_json(d / "props.json", props)
    out = d / "out"; out.mkdir(exist_ok=True)
    name = f"{slug}-preview.mp4" if preview else f"{slug}.mp4"
    subprocess.run(["node", "render-long.mjs", str(d / "props.json"), str(d), str(out / name), "LongForm"],
                   cwd=ROOT / "remotion", check=True, shell=os.name == "nt")
    th = s.get("thumbnail")
    split = [next(iter(sorted((d / "assets").glob(n + ".*"))), None) for n in ("thumb_game", "01_real")]
    if th and ((d / "thumb_bg.jpg").exists() or all(split)) and not preview:
        tj = {"bg": "thumb_bg.jpg", "text": th.get("text", ""), "accent": th.get("accent", "")}
        if all(split):  # assets/thumb_game.* + assets/01_real.* -> split GAME | REAL thumbnail
            tj.update(left=f"assets/{split[0].name}", right=f"assets/{split[1].name}")
        save_json(d / "thumb.json", tj)
        subprocess.run(["node", "render-long.mjs", str(d / "thumb.json"), str(d), str(out / "thumbnail.png"), "Thumbnail"],
                       cwd=ROOT / "remotion", check=True, shell=os.name == "nt")
    _description(d, s, chapters, timeline)
    print("done ->", out / name)


def _description(d: Path, s: dict, chapters, timeline) -> None:
    lines = [s["title"], "", s.get("description", "").strip(), ""]
    if chapters:
        lines.append("0:00 Intro" if chapters[0]["start"] > 0 else "")
        for c in chapters:
            m, sec = divmod(int(c["start"]), 60)
            lines.append(f"{m}:{sec:02d} {c['title']}")
        lines.append("")
    if any(x.get("credit") == "stock" for x in timeline):
        lines.append("Stock footage: Pixabay (pixabay.com)." if any("pixabay_" in str(x.get("src", "")) for x in timeline) or env("PIXABAY_API_KEY") else "Stock footage: Pexels (pexels.com).")
    if credits := _credits(d):
        lines += ["Gameplay footage (Creative Commons Attribution):", *credits]
    if (cf := d / "assets" / "credits.txt").exists():  # one line per photo, written by the user
        lines += ["Photos:", *[x.strip() for x in cf.read_text(encoding="utf-8").splitlines() if x.strip()]]
    lines.append("Narration is AI voice; some illustrations are AI-generated. Script researched and written for this channel.")
    if music := list((d / "music").glob("*")) if (d / "music").exists() else []:
        lines.append("Music: " + ", ".join(sorted({m.stem for m in music})))
    (d / "out" / "description.txt").write_text("\n".join(x for x in lines if x is not None), encoding="utf-8")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    cmd, slug = args[0], (args[1] if len(args) > 1 else None)
    if cmd == "voices":
        return cmd_voices()
    if cmd == "all":
        for step in (cmd_check, cmd_voice, cmd_images, cmd_fetch, cmd_render):
            step(slug)
        return
    if cmd == "lint":  # lf.py lint <slug> <draft.txt> [assets dir]
        return cmd_lint(slug, args[2], args[3] if len(args) > 3 else None)
    if cmd == "render":
        preview = args[2] if len(args) > 2 and args[2].replace(".", "").isdigit() else False
        return cmd_render(slug, preview)
    {"sheets": cmd_sheets, "tag": cmd_tag, "build": cmd_build, "check": cmd_check, "voice": cmd_voice, "images": cmd_images, "fetch": cmd_fetch,
     "prompts": cmd_prompts, "el-voices": cmd_el_voices, "el-find": cmd_el_find,
     "el-add": cmd_el_add}[cmd](" ".join(args[1:]) if cmd == "el-find" else slug)


if __name__ == "__main__":
    main()
