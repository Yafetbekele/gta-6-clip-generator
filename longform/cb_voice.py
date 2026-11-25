"""Chatterbox TTS worker, run with .venv-voice: python cb_voice.py job.json

job.json: {"items": [{"text": "...", "out": "path.wav", "seed": 0}], "exaggeration": 0.5,
           "cfg_weight": 0.5, "temperature": 0.8, "ref": "optional voice sample .wav"}
"""
import json, sys

import torch, torchaudio
from chatterbox.tts import ChatterboxTTS

job = json.load(open(sys.argv[1], encoding="utf-8"))
model = ChatterboxTTS.from_pretrained(device="cuda" if torch.cuda.is_available() else "cpu")
if job.get("ref"):
    model.prepare_conditionals(job["ref"], exaggeration=job.get("exaggeration", 0.5))
for n, item in enumerate(job["items"], 1):
    torch.manual_seed(item.get("seed", 0))
    wav = model.generate(item["text"], exaggeration=job.get("exaggeration", 0.5),
                         cfg_weight=job.get("cfg_weight", 0.5), temperature=job.get("temperature", 0.8))
    torchaudio.save(item["out"], wav.cpu(), model.sr)
    print(f"tts {n}/{len(job['items'])}", flush=True)
