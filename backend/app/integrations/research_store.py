from __future__ import annotations

import json
import time
from pathlib import Path

# Durable store for Research Lab artifacts (spread studies and trained
# presets). The bot accumulates what it learned across restarts: every study
# and every training run lands in .aurelion/research/ as a self-contained JSON
# file, and the Lab lists them so a previously learned preset can be
# re-applied in one click. Best-effort disk IO — never raises into the caller.

RESEARCH_DIR = Path(".aurelion/research")
KINDS = ("spread-study", "autotune")


def save_research(kind: str, payload: dict) -> str | None:
    if kind not in KINDS or not isinstance(payload, dict):
        return None
    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        stamp = payload.get("generatedAt") or int(time.time() * 1000)
        name = f"{kind}_{stamp}.json"
        (RESEARCH_DIR / name).write_text(json.dumps({"kind": kind, "payload": payload}), encoding="utf-8")
        return name
    except OSError:
        return None


def _headline(kind: str, payload: dict) -> str:
    if kind == "autotune":
        baseline = payload.get("baseline") or {}
        best = payload.get("best") or {}
        mode = "robust" if payload.get("robust") else (payload.get("regime") or "normal")
        return (
            f"{mode} / {payload.get('source')}: score {baseline.get('score')} -> "
            f"{best.get('validationScore', best.get('score'))} (validation), "
            f"{'improved' if payload.get('improvedVsBaseline') else 'no improvement'}"
        )
    summary = payload.get("summary") or {}
    half_life = summary.get("medianHalfLifeMs")
    pretty = f"{round(half_life / 1000)} s" if half_life else "n/a"
    return (
        f"{payload.get('pairsFitted')}/{payload.get('pairsTotal')} pairs, median half-life {pretty}, "
        f"{summary.get('executableEpisodes', 0)} episode(s) beat the fee wall"
    )


def load_research(limit: int = 12) -> list[dict]:
    entries: list[dict] = []
    try:
        files = sorted(RESEARCH_DIR.glob("*.json"), reverse=True)
    except OSError:
        return entries
    for path in files[: max(1, limit)]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            kind = raw.get("kind")
            payload = raw.get("payload") or {}
            if kind not in KINDS:
                continue
            entries.append({
                "file": path.name,
                "kind": kind,
                "generatedAt": payload.get("generatedAt"),
                "headline": _headline(kind, payload),
                "payload": payload,
            })
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda entry: entry.get("generatedAt") or 0, reverse=True)
    return entries
