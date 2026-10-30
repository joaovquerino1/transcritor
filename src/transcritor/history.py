"""Histórico de transcrições: o que foi transcrito continua na lista ao reabrir o app.

Um índice pequeno (index.json) é lido na abertura; cada transcrição fica no seu próprio
arquivo e só é carregada quando selecionada.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from . import models
from .engine import Transcript, Utterance, Word

MAX_ITEMS = 200


def _dir() -> Path:
    # TRANSCRITOR_HISTORICO: pasta alternativa (testes automáticos não mexem no histórico real)
    d = Path(os.environ.get("TRANSCRITOR_HISTORICO") or models.data_dir() / "historico")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _dir() / "index.json"


def load_index() -> list[dict]:
    try:
        items = json.loads(_index_path().read_text(encoding="utf-8"))
        return [i for i in items if (_dir() / f"{i['id']}.json").exists()]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _write_index(items: list[dict]) -> None:
    tmp = _index_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_index_path())


def new_id(path: str) -> str:
    return hashlib.sha1(f"{path}|{time.time()}".encode()).hexdigest()[:12]


def save(hid: str, path: str, outputs: list[str], tr: Transcript) -> None:
    data = {
        "source": tr.source, "language": tr.language, "duration": tr.duration, "model": tr.model,
        "diarized": tr.diarized, "speaker_names": {str(k): v for k, v in tr.speaker_names.items()},
        "utterances": [{"start": u.start, "end": u.end, "speaker": u.speaker, "text": u.text,
                        "words": [[w.start, w.end, w.text, w.speaker] for w in u.words]}
                       for u in tr.utterances],
    }
    tmp = _dir() / f"{hid}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_dir() / f"{hid}.json")
    items = [i for i in load_index() if i["id"] != hid]
    items.insert(0, {"id": hid, "path": path, "outputs": outputs, "duration": tr.duration,
                     "speakers": len(tr.speaker_names) if tr.diarized else 0,
                     "saved": time.strftime("%Y-%m-%d %H:%M")})
    for old in items[MAX_ITEMS:]:
        (_dir() / f"{old['id']}.json").unlink(missing_ok=True)
    _write_index(items[:MAX_ITEMS])


def load(hid: str) -> Optional[Transcript]:
    try:
        d = json.loads((_dir() / f"{hid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    utts = [Utterance(u["start"], u["end"], u["speaker"], u["text"],
                      [Word(*w) for w in u.get("words", [])]) for u in d["utterances"]]
    return Transcript(d["source"], d["language"], d["duration"], d["model"], utts,
                      {int(k): v for k, v in d["speaker_names"].items()}, d["diarized"])


def delete(hids: list[str]) -> None:
    if not hids:
        return
    for h in hids:
        (_dir() / f"{h}.json").unlink(missing_ok=True)
    _write_index([i for i in load_index() if i["id"] not in hids])
