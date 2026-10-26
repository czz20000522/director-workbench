"""Prompt-stated output quality overrides the picker, as an explicit deterministic rule.

A prompt that names 1K/2K/4K/8K (or 高清/超高清 and close synonyms) is treated as the
operator's instruction; the selected tier is ignored for that submission.  The rule is
applied by the backend, so every HTTP/MCP caller gets it, and the decision is recorded
on the task instead of being silently applied.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_RULES_PATH = Path(__file__).resolve().parent.parent / 'shared' / 'quality-intent.json'
_RULES = json.loads(_RULES_PATH.read_text(encoding='utf-8'))

_TIERS = _RULES['tiers']
_ORDER = {tier['resolution']: index for index, tier in enumerate(_TIERS)}
_LABELS = {tier['resolution']: tier['label'] for tier in _TIERS}
_TERMS = sorted(
    ((tier['resolution'], phrase) for tier in _TIERS for phrase in tier['phrases']),
    key=lambda item: len(item[1]),
    reverse=True,
)
_NEGATIONS = _RULES['negations']
_VIDEO_MAX = _RULES['video']['max_resolution']


def label(resolution: str) -> str:
    return _LABELS.get(resolution, resolution.upper())


def _is_ascii(value: str) -> bool:
    return all(ord(char) < 128 for char in value)


def _negated(prefix: str) -> bool:
    probe = prefix.rstrip(' \t，,。.;；:：!！?？')
    if any(probe.endswith(item) for item in _NEGATIONS['prefixes']):
        return True
    if probe and probe[-1] in _NEGATIONS['chars']:
        return True
    words = _NEGATIONS['words']
    return bool(words and re.search(r'(?<![a-z])(?:' + '|'.join(words) + r')(?![a-z])', probe))


def detect(prompt: str | None) -> dict[str, Any] | None:
    """Return the highest tier named by the prompt, or None when it names no tier."""
    text = str(prompt or '').casefold()
    if not text.strip():
        return None
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, str]] = []
    for resolution, phrase in _TERMS:
        needle = phrase.casefold()
        pattern = (re.compile(r'(?<![0-9a-z])' + re.escape(needle) + r'(?![0-9a-z])')
                   if _is_ascii(phrase) else re.compile(re.escape(needle)))
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < other_end and other_start < end for other_start, other_end in taken):
                continue
            if _negated(text[max(0, start - 6):start]):
                continue
            taken.append((start, end))
            found.append((_ORDER[resolution], start, resolution, phrase))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1]))
    _, _, resolution, phrase = found[0]
    return {'resolution': resolution, 'label': label(resolution), 'phrase': phrase}


def resolve(prompt: str | None, selected: str, *, video: bool = False) -> tuple[str, dict[str, Any] | None]:
    """Return the applied resolution and a record explaining any override or limit."""
    intent = detect(prompt)
    requested = intent['resolution'] if intent else selected
    applied = requested
    if video and requested == '8k':
        applied = _VIDEO_MAX
    record: dict[str, Any] | None = None
    if intent or applied != requested:
        record = {
            'source': 'prompt' if intent else 'limit',
            'requested': requested,
            'requested_label': label(requested),
            'applied': applied,
            'applied_label': label(applied),
            'phrase': intent['phrase'] if intent else None,
            'capped': applied != requested,
        }
        if applied != requested:
            record['reason'] = '8K 视频需要数十分钟 CPU 转码且文件过大，本次按 4K 输出；原生文件仍会保留。'
    return applied, record
