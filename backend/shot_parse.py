"""把一段（可能很乱的）文字拆成分镜提议。

边界写死在这里，和「素材清单」「润色」「识图」分开：

* 只做拆分与整理，不改剧情、不补人物、不加原文没有的镜头语言；
* 景别/机位/地点/表演/服装只在原文写了才填，没写就留空，让界面显示「未指定」；
* 对白和声音要求留在 prompt 里，不单独抽走。

输出仅用于界面预览，用户勾选后才由 /plan/segments 真正落库。
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_SHOTS = 12
MAX_PROMPT = 2000
MAX_FIELD = 120

SHOT_PROMPT = """你是分镜师。用户会给一段可能很乱的文字，里面可能混着剧情、角色、场景、
镜头、素材要求和对白。你的任务只有一个：把它拆成可以直接拿去生成视频的镜头列表。

只输出一个 JSON 对象，不要解释、不要 Markdown 代码块：
{"shots":[{"prompt":"这个镜头的画面与动作描述","duration_seconds":4,
 "shot_size":"景别","camera":"机位或运镜","location":"地点",
 "performance":"表演","wardrobe":"服装"}]}

规则：
- 按叙事顺序拆。出现换地点、换时间、换视角，或原文明确写了「切」「镜头」时，拆成两条。
- prompt 尽量保留原文措辞与细节；你只做整理，不做润色，不添加原文没有的元素。
- shot_size / camera / location / performance / wardrobe 只在原文明确写了才填；
  没写就空字符串，不要根据常识猜。
- wardrobe 只放服装本身；发型、随身物品、道具留在 prompt 里，不要塞进 wardrobe。
- performance 只放动作、表情、说话方式；不要复述剧情，也不要重复 prompt。
- duration_seconds 只在原文写了时长时给数字；没写就给 0，由界面用默认时长。
- 对白、音效、配乐要求保留在 prompt 里，不要单独拆出去。
- 最多 12 条；宁可少拆，也不要把一句话硬切成两条。"""


def extract_json(text: str) -> dict[str, Any]:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[A-Za-z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('模型没有返回可解析的 JSON')
    parsed = json.loads(raw[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError('分镜结果不是对象格式')
    return parsed


def _text(value: Any, limit: int) -> str:
    return str(value or '').strip()[:limit]


def shots_from_payload(payload: Any, *, limit: int = MAX_SHOTS) -> list[dict[str, Any]]:
    rows = payload.get('shots') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('模型返回里没有 shots 数组')
    shots: list[dict[str, Any]] = []
    for raw in rows[:max(1, min(limit, MAX_SHOTS))]:
        if not isinstance(raw, dict):
            continue
        prompt = _text(raw.get('prompt'), MAX_PROMPT)
        if not prompt:
            continue
        duration = raw.get('duration_seconds')
        if type(duration) not in (int, float) or not (1 <= float(duration) <= 15):
            duration = 0
        shots.append({
            'prompt': prompt,
            'duration_seconds': int(duration) if float(duration).is_integer() else round(float(duration), 1),
            'shot_size': _text(raw.get('shot_size'), MAX_FIELD),
            'camera': _text(raw.get('camera'), MAX_FIELD),
            'location': _text(raw.get('location'), MAX_FIELD),
            'performance': _text(raw.get('performance'), MAX_FIELD),
            'wardrobe': _text(raw.get('wardrobe'), MAX_FIELD),
        })
    return shots
