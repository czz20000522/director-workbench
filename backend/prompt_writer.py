"""提示词代写：把一句想法写成分镜模型能直接吃的画面描述。

两条通道，由用户在前端显式选择：

- ``local``：本机 Ollama 上的无审查模型。成人向的身体与裸露描述在云端 API 上会被
  拒答或"净化"，所以这条链路固定打 ``127.0.0.1:11434``，没有 key、没有云端回退。
- ``online``：任意 OpenAI 兼容端点（默认 DeepSeek）。用户自己填 Base URL / 模型 /
  API Key，配置存在本机数据根，不进仓库。在线通道受平台规则约束，可能拒答成人向内容，
  这一点在界面上写明，不替用户隐瞒。

Ollama 是**点生成才拉起**：工作台常驻时不碰 11434。写完按 ``keep_alive``（默认 30s）
自动退显存，出片前还可以主动 ``release()`` 立刻腾卡。

产出不落盘：草稿只回给界面，不进任务回执、不进日志、不写数据库。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import image_formats

SERVICE_URL = os.environ.get('DIRECTOR_PROMPT_URL', 'http://127.0.0.1:11434').rstrip('/')
# 首选 9B-Q4（约 6.2 GB）：中文原生，16 GB 卡上写完就退，不和出片抢显存。
# 注意必须带 huihui_ai/ 命名空间：Ollama 里非官方模型都在命名空间下，写短名永远匹配不上。
MODEL_ID = os.environ.get('DIRECTOR_PROMPT_MODEL', 'huihui_ai/qwen3.5-abliterated:9b')
# 写完 30 秒退显存：连续改几版不用反复加载，停下来半分钟就自动让位给 H3。
DEFAULT_KEEP_ALIVE = os.environ.get('DIRECTOR_PROMPT_KEEP_ALIVE', '30s')
TIMEOUT = float(os.environ.get('DIRECTOR_PROMPT_TIMEOUT', '300'))
ONLINE_TIMEOUT = float(os.environ.get('DIRECTOR_PROMPT_ONLINE_TIMEOUT', '120'))
MAX_IDEA = 1200
# 识图请求的上下文窗口。Ollama 默认 4096，装不下「缩到 1024 长边的图 + 指令」，
# 会直接 400 报 exceeds the available context size。
VISION_NUM_CTX = int(os.environ.get('DIRECTOR_PROMPT_VISION_CTX', '8192'))
MAX_OUTPUT = 900
# 识图：单张图太大时 base64 会把请求撑爆，也给本地模型添无谓的编码开销。
MAX_IMAGE_BYTES = 16 * 1024**2

DESCRIBE_INSTRUCTION = """看这张图，写一段能用来重新生成类似画面的中文提示词。

按这个顺序写：主体与外观 → 动作与姿态 → 镜头（景别、机位、角度）→ 光线与色温
→ 材质与纹理 → 环境与背景。

要求：
1. 一段话，中文，80 到 160 字，不分点、不加标题。
2. 只写画面里看得见的东西；不要评价、不要猜剧情、不要写心理活动。
3. 不要用「这张图」「画面中」开头，直接写内容。
4. 不写品牌、真实人名、真实地名。
5. 只输出提示词本身，不加引号、不加「提示词：」前缀。"""
# 识图有两个用途，要求正好相反：
# * 描述成片 → 要连光线、材质一起写，才能复现；
# * 给参考图补信息 → 只该说人物和构图，因为参考图常是深度图/线稿/3D 人偶，
#   把它们的「磨砂质感、灰阶」写进提示词会污染生成结果（实测会真的生成人偶）。
REFERENCE_DESCRIBE_INSTRUCTION = """看这张参考图，只提取和「人物与构图」有关的事实，写成一段中文。
按这个顺序写：画面里有几个人 → 每个人的外观（发型、服装的款式/颜色/材质）→ 各自的姿态与相互位置 → 景别与机位。
要求：
1. 一段话，中文，60 到 120 字，不分点、不加标题。
2. 只写人物和构图。不要写光线、色调、质感、渲染风格、背景美术——参考图可能只是线稿或深度图，那些不是真实内容。
3. 看不清就略过，不要编造。
4. 直接写内容，不要用「这张图」开头，只输出这段文字本身。"""
STARTUP_TIMEOUT = float(os.environ.get('DIRECTOR_PROMPT_STARTUP_TIMEOUT', '45'))
_START_LOCK = threading.Lock()
# 出片前会被连续调用好几次（批量提交），这个时间戳用来去重。
_LAST_RELEASE = 0.0

DEFAULT_ONLINE = {
    'base_url': 'https://api.deepseek.com/v1',
    'model': 'deepseek-chat',
}

SYSTEM_PROMPT = """你是中文影像提示词写手，为一个虚构影视项目写画面描述，人物都是成年人。

把你收到的一句话想法，写成拍摄/生成模型能直接用的画面描述，遵守这些规则：

1. 只写镜头里看得见的东西，顺序是：主体与外观 → 动作与姿态 → 镜头（景别、机位、运动）
   → 光线与色温 → 材质与纹理 → 环境里可见的细节。
2. 一段话，中文，90 到 160 字。不分点、不加标题、不解释你在做什么。
3. 允许直白地写成年人的身体、裸露程度、情欲氛围和衣物状态；用具体的形、色、材质、
   光影去写，不要写成器官清单，也不写剧情、心理活动和对白。
4. 不写"电影感""高级感""大片""氛围感拉满"这类空词，用能拍出来的细节替代。
5. 不写品牌、真实人名、真实地名。
6. 想法里已经给出的细节要保留，不要替换成更保守的说法。
7. 只输出描述本身：不加引号，不加"提示词："之类的前缀。

如果想法涉及未成年人，直接输出：无法处理。"""

POLISH_PROMPT = """你是中文影像提示词的润色编辑。用户会给你一段他自己写的提示词，
你要在不改变原意、不删掉任何具体细节的前提下把它改好。

规则：

1. 用户写的人物长相、服装、动作、场景、镜头、光线、道具——一个都不能丢，也不能换成别的。
2. 补上原文缺的、但画面必须有的：景别与机位、镜头运动、光的方向与色温、材质与纹理
   （只限衣物、家具、墙面这类看得见的东西）、环境里可见的细节。
   不要给人物新增原文没有的皮肤状态、身体特征或表情。
3. 删掉"电影感""高级感""大片""氛围感拉满"这类空词，换成能拍出来的具体描述。
4. 理顺语序：主体与外观 → 动作与姿态 → 镜头 → 光线 → 材质 → 环境。
5. 一段话，中文，90 到 200 字。不分点、不加标题、不解释你改了什么、不输出修改说明。
6. 允许直白保留成年人的身体、裸露程度与情欲氛围，不要替用户"净化"或改成更保守的说法。
7. 只输出润色后的描述本身：不加引号，不加"提示词："之类的前缀。

如果原文涉及未成年人，直接输出：无法处理。"""

TRANSLATE_PROMPT = """你是影像提示词的中译英译者。用户会给你一段中文提示词，请译成英文。

规则：

1. 逐项对应：人物长相、服装、动作、姿态、场景、道具、镜头、光线、材质、氛围，一个都不能漏，
   也不要合并成概括。
2. 不添加原文没有的内容，不解释、不扩写、不评价、不加标题。
3. 沿用原文的顺序：主体与外观 → 动作与姿态 → 镜头 → 光线 → 材质 → 环境。
4. 专业词用影像行业常见说法：全身 → full-body shot；中景 → medium shot；特写 → close-up；
   浅景深 → shallow depth of field；侧逆光 → side backlight；柔光 → soft light；
   低角度仰拍 → low-angle shot；俯拍 → high-angle shot。
5. 数字、比例、尺寸（如 16:9、768×1344）和专有名词原样保留。
6. 只输出英文译文本身：不加引号，不加「Translation:」之类前缀，不要保留中文原句，
   不要输出任何说明。
7. 原文允许的成年人身体、裸露程度与情欲氛围照实翻译，不要替用户净化或改成更保守的说法。

如果原文涉及未成年人的性内容，直接输出：无法处理。"""

_LABEL = re.compile(r'^\s*(?:提示词|画面描述|描述|prompt)\s*[:：]\s*', re.IGNORECASE)
# qwen3 是思考模型：即便请求里关了 think，也可能漏出思考段，这里两种闭合符都认。
_THINK_BLOCK = re.compile(r'<think(?:ing)?>.*?(?:</think(?:ing)?>|<\uff5cend\u2581of\u2581thinking\uff5c>)',
                          re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r'<think(?:ing)?>.*$', re.DOTALL | re.IGNORECASE)


class WriterUnavailable(RuntimeError):
    """服务没跑、模型没装、没填 Key——是环境问题，不是请求问题。"""


class WriterFailed(RuntimeError):
    """连上了但写不出来。"""


# ---------------------------------------------------------------- 配置

def config_path() -> Path:
    """配置跟着数据根走：``director-data/config/prompt-writer.json``。

    里面可能有在线 API Key，所以只落本机数据根（不进仓库、不进 git）。
    """
    from . import app as backend
    return Path(backend.ROOT) / 'config' / 'prompt-writer.json'


def _blank_config() -> dict[str, Any]:
    return {
        'provider': 'local',
        'local': {'model': '', 'keep_alive': DEFAULT_KEEP_ALIVE},
        'online': {**DEFAULT_ONLINE, 'api_key': ''},
    }


def load_config() -> dict[str, Any]:
    document = _blank_config()
    target = config_path()
    try:
        stored = json.loads(target.read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, ValueError):
        return document
    if not isinstance(stored, dict):
        return document
    if stored.get('provider') in ('local', 'online'):
        document['provider'] = stored['provider']
    local = stored.get('local')
    if isinstance(local, dict):
        if isinstance(local.get('model'), str):
            document['local']['model'] = local['model'].strip()[:120]
        if isinstance(local.get('keep_alive'), str) and local['keep_alive'].strip():
            document['local']['keep_alive'] = local['keep_alive'].strip()[:16]
    online = stored.get('online')
    if isinstance(online, dict):
        for key in ('base_url', 'model', 'api_key'):
            if isinstance(online.get(key), str):
                document['online'][key] = online[key].strip()[:400]
    return document


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """只接受白名单字段；``api_key`` 传 None 表示不动，传空串表示清除。"""
    document = load_config()
    provider = patch.get('provider')
    if provider in ('local', 'online'):
        document['provider'] = provider
    local = patch.get('local')
    if isinstance(local, dict):
        model = local.get('model')
        if isinstance(model, str):
            document['local']['model'] = model.strip()[:120]
        keep = local.get('keep_alive')
        if isinstance(keep, str) and keep.strip():
            document['local']['keep_alive'] = keep.strip()[:16]
    online = patch.get('online')
    if isinstance(online, dict):
        base_url = online.get('base_url')
        if isinstance(base_url, str) and base_url.strip():
            document['online']['base_url'] = base_url.strip().rstrip('/')[:400]
        model = online.get('model')
        if isinstance(model, str) and model.strip():
            document['online']['model'] = model.strip()[:120]
        key = online.get('api_key')
        if isinstance(key, str):
            document['online']['api_key'] = key.strip()[:400]
    target = config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f'{target.name}.tmp')
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)
    except OSError as exc:
        raise WriterFailed(f'配置写不进去：{exc}') from exc
    return document


def local_model() -> str:
    configured = load_config()['local'].get('model') or ''
    return configured or MODEL_ID


def keep_alive_value() -> str:
    return load_config()['local'].get('keep_alive') or DEFAULT_KEEP_ALIVE


def online_key() -> str:
    """配置里的 Key 优先；没填就退回环境变量，方便命令行临时用。"""
    configured = load_config()['online'].get('api_key') or ''
    return configured or os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('OPENAI_API_KEY') or ''


def _mask_key(key: str) -> str:
    if not key:
        return ''
    if len(key) <= 10:
        return '****'
    return f'{key[:5]}…{key[-4:]}'


def config_view() -> dict[str, Any]:
    """给界面的配置视图：**永远不返回完整 Key**，只说有没有、后四位是什么。"""
    document = load_config()
    key = online_key()
    return {
        'provider': document['provider'],
        'local': {**document['local'], 'model': local_model()},
        'online': {**document['online'], 'api_key': '',
                   'has_key': bool(key), 'key_hint': _mask_key(key)},
    }


# ---------------------------------------------------------------- 本机服务

def _models_root() -> Path:
    """找真正装着模型的目录。

    Ollama 安装版会把 ``OLLAMA_MODELS`` 写进用户环境变量，但工作台进程可能是
    在改变量之前启动的，手里还是旧值。所以这里按「谁真的有 manifests 目录」
    依次探一遍，全部落空才退回环境变量。拉扯服务时要把这个值显式传给子进程，
    否则新起的 ollama 会去空目录里找模型，看起来像「模型丢了」。
    """
    candidates: list[Path] = []
    env = os.environ.get('OLLAMA_MODELS')
    if env:
        candidates.append(Path(env))
    candidates.append(Path('D:/AI/Ollama/models'))
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(Path(local) / 'Ollama' / 'models')
    candidates.append(Path.home() / '.ollama' / 'models')
    for candidate in candidates:
        try:
            if (candidate / 'manifests').is_dir():
                return candidate
        except OSError:
            continue
    return candidates[0] if candidates else Path.home() / '.ollama' / 'models'


def _model_manifest(model: str) -> Path | None:
    """直接查模型仓库：服务停着也不该被当成「模型没装」。

    Ollama 的清单路径分两种：官方模型在 ``<ns=library>/<name>/<tag>``，
    带命名空间的在 ``<ns>/<name>/<tag>``（如 huihui_ai/qwen3-abliterated/8b）。
    """
    root = _models_root()
    parts = [part for part in str(model).split('/') if part]
    if not parts:
        return None
    name, _, tag = parts[-1].partition(':')
    namespace = parts[0] if len(parts) > 1 else 'library'
    if not name:
        return None
    try:
        for candidate in (root / 'manifests').glob(f'*/{namespace}/{name.lower()}/{tag or "latest"}'):
            if candidate.exists():
                return candidate
    except OSError:
        return None
    return None


def find_executable() -> str | None:
    """定位 ollama.exe：先看显式配置，再看 PATH，最后试装机常见位置。"""
    configured = os.environ.get('DIRECTOR_OLLAMA_EXE')
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which('ollama')
    if found:
        return found
    local = os.environ.get('LOCALAPPDATA')
    candidates = [
        r'D:\AI\Ollama\ollama.exe',
        r'D:\Ollama\ollama.exe',
        r'C:\Program Files\Ollama\ollama.exe',
    ]
    if local:
        candidates.insert(0, str(Path(local) / 'Programs' / 'Ollama' / 'ollama.exe'))
    for candidate in candidates:
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return None


def _service_models(*, timeout: float = 3.0) -> list[str]:
    # trust_env=False：回环地址不能被系统代理接管，否则健康的 127.0.0.1 会变成 502。
    response = httpx.get(f'{SERVICE_URL}/api/tags', timeout=timeout, trust_env=False)
    response.raise_for_status()
    payload = response.json()
    models = payload.get('models') if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [str(item.get('name')) for item in models if isinstance(item, dict) and item.get('name')]


def service_alive(timeout: float = 2.0) -> bool:
    try:
        _service_models(timeout=timeout)
        return True
    except (OSError, ValueError, httpx.HTTPError):
        return False


def ensure_service(*, wait_seconds: float | None = None) -> dict[str, Any]:
    """服务没跑就现拉一个，跑着就直接用。

    这就是「点生成才拉起」的落点：工作台启动时不碰 Ollama，用户不点生成，
    11434 上什么都没有，显存里也不会留一份模型。拉起的是独立进程（开机不自启），
    写完按 keep_alive 退显存，进程本身留着也不占显卡。
    """
    limit = STARTUP_TIMEOUT if wait_seconds is None else wait_seconds
    if service_alive():
        return {'started': False, 'wait_ms': 0, 'pid': None}
    executable = find_executable()
    if not executable:
        raise WriterUnavailable(
            '没找到 ollama.exe。装好 Ollama 后重试，或用 DIRECTOR_OLLAMA_EXE 指定路径。')
    with _START_LOCK:
        # 两个请求同时点生成时，后到的那个不该再拉一份。
        if service_alive():
            return {'started': False, 'wait_ms': 0, 'pid': None}
        environment = dict(os.environ)
        environment['OLLAMA_MODELS'] = str(_models_root())
        # 拉起的实例必须监听我们探测的地址；否则 DIRECTOR_PROMPT_URL 换了端口，
        # 新服务还是蹲在 11434，这边永远等不到。
        parsed = urlparse(SERVICE_URL)
        environment['OLLAMA_HOST'] = f'{parsed.hostname or "127.0.0.1"}:{parsed.port or 11434}'
        flags = 0
        if os.name == 'nt':
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(
                subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603 - 路径来自本机固定探测
                [executable, 'serve'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, creationflags=flags, close_fds=True,
            )
        except OSError as exc:
            raise WriterUnavailable(f'拉起 ollama serve 失败：{exc}') from exc
        while time.monotonic() - started_at < limit:
            if service_alive(timeout=1.0):
                return {'started': True, 'wait_ms': int((time.monotonic() - started_at) * 1000),
                        'pid': process.pid}
            if process.poll() is not None:
                # 可能是另一个实例抢先监听了端口：进程退出但端口通了，也算成功。
                time.sleep(0.3)
                if service_alive(timeout=1.0):
                    return {'started': True,
                            'wait_ms': int((time.monotonic() - started_at) * 1000), 'pid': None}
                raise WriterUnavailable(
                    'ollama serve 启动后立刻退出。在命令行执行一次 `ollama serve` 看报错。')
            time.sleep(0.35)
        raise WriterUnavailable(f'拉起本机服务超时（{limit:.0f} 秒），在命令行执行 `ollama serve` 看卡在哪。')


def _loaded_models(*, timeout: float = 2.0) -> list[str]:
    """``/api/ps`` 只列**真正驻留显存**的模型。

    必须先看这个再决定要不要发卸载请求：对着一个没加载的模型发 ``keep_alive: 0``，
    Ollama 会先把 6 GB 权重读进来再卸掉——白白占一次显存和十几秒。
    """
    try:
        response = httpx.get(f'{SERVICE_URL}/api/ps', timeout=timeout, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except (OSError, ValueError, httpx.HTTPError):
        return []
    models = payload.get('models') if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [str(item.get('name') or item.get('model') or '') for item in models if isinstance(item, dict)]


def release(timeout: float = 8.0) -> dict[str, Any]:
    """把模型从显存里请出去。

    出片前调用一次：16 GB 卡装不下 9B 写词模型 + H3，靠 keep_alive 自己到点退
    不够及时。请求里 ``keep_alive: 0`` 会让 Ollama 在回包后立刻卸载权重。

    批量提交时会被连续调用（12 张图 = 12 次），所以 8 秒内只真做一次。
    """
    global _LAST_RELEASE
    now = time.monotonic()
    if now - _LAST_RELEASE < 8:
        return {'released': True, 'reason': '刚刚卸过'}
    _LAST_RELEASE = now
    if not service_alive(timeout=1.5):
        return {'released': False, 'reason': '服务没在运行'}
    model = active_model()
    loaded = _loaded_models()
    if loaded and model not in loaded:
        return {'released': True, 'model': model, 'reason': f'驻留的是 {", ".join(loaded)}'}
    if not loaded:
        return {'released': True, 'model': model, 'reason': '本来就没加载'}
    try:
        response = httpx.post(f'{SERVICE_URL}/api/generate',
                              json={'model': model, 'prompt': '', 'keep_alive': 0},
                              timeout=timeout, trust_env=False)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        return {'released': False, 'reason': str(exc)[:120]}
    return {'released': response.status_code < 400, 'model': model}


def release_quietly() -> None:
    """任务提交前调用：卸不掉也不能挡住出片。"""
    try:
        release()
    except Exception:  # noqa: BLE001 - 卸载是尽力而为，绝不阻断生成
        pass


# ---------------------------------------------------------------- 模型选择

def _family(model: str) -> str:
    return str(model).split(':')[0]


def _namespace(model: str) -> str:
    parts = [part for part in str(model).split('/') if part]
    return parts[0].casefold() if len(parts) > 1 else 'library'


def _flavour(model: str) -> str:
    """``qwen3.5-abliterated`` → ``abliterated``：跨小版本认亲用。"""
    return _family(model).split('/')[-1].rsplit('-', 1)[-1].casefold()


# embedding 模型也会出现在 /api/tags 里，写词时选中它只会得到一个向量。
_EMBEDDING_HINTS = ('embed', 'bge-', 'nomic', 'gte-', 'e5-', 'paraphrase')


def _usable(name: str) -> bool:
    lowered = str(name).casefold()
    return not any(hint in lowered for hint in _EMBEDDING_HINTS)


def active_model(installed: list[str] | None = None) -> str:
    """默认档没装时，退到本机真正装了的那一档。

    用户可能在别处拉 4B/9B/14B，甚至同一作者换了小版本号（``qwen3`` →
    ``qwen3.5``）：只要还是同一个 abliterated 系列，写词都能用，不该因为标签
    不同就判成「没装」。显式设了 DIRECTOR_PROMPT_MODEL 的话，那个值仍然优先。
    """
    preferred = local_model()
    if installed is None:
        try:
            installed = _service_models()
        except (OSError, ValueError, httpx.HTTPError):
            installed = []
    if preferred in installed:
        return preferred
    pool = [name for name in installed if _usable(name)]
    family = _family(preferred)
    for name in pool:
        if _family(name) == family:
            return name
    namespace, flavour = _namespace(preferred), _flavour(preferred)
    for name in pool:
        if _namespace(name) == namespace and _flavour(name) == flavour:
            return name
    for name in pool:
        if _namespace(name) == namespace and 'abliterated' in name.casefold():
            return name
    # 只剩一个也说不好它是不是视觉/工具模型，宁可报「没装」让人自己指定，
    # 也不要静默拿一个不对的模型写出半成品（qwen2.5vl 也出现在 /api/tags 里）。
    return preferred


def _local_state() -> dict[str, Any]:
    try:
        names = _service_models()
        running = True
    except (OSError, ValueError, httpx.HTTPError):
        names, running = [], False
    model = active_model(names)
    executable = find_executable()
    base = {'model': model, 'service': SERVICE_URL, 'installed': names,
            'can_start': bool(executable), 'executable': executable or ''}
    if running and model in names:
        return {**base, 'available': True, 'running': True, 'reason': '',
                'hint': '点生成就出词；服务已在本机运行。'}
    if _model_manifest(model):
        return {**base, 'available': True, 'running': running,
                'reason': '' if running else '服务没启动，点生成会自动拉起。',
                'hint': '模型已下载，首次调用要加载约 10 秒。'}
    if not running and not executable:
        return {**base, 'available': False, 'running': False,
                'reason': '本机没找到 ollama.exe，先装 Ollama 再回来。', 'hint': ''}
    return {**base, 'available': False, 'running': running,
            'reason': f'本机还没下载 {model}，执行：ollama pull {model}', 'hint': ''}


def _online_headers(key: str) -> dict[str, str]:
    # opencode.ai 的 zen 网关需要会话头；对标准 OpenAI 兼容端点来说这是未知头，会被忽略。
    return {'Authorization': f'Bearer {key}', 'x-opencode-session': str(uuid.uuid4())}


def _online_state() -> dict[str, Any]:
    settings = load_config()['online']
    key = online_key()
    base = {'model': settings.get('model') or DEFAULT_ONLINE['model'],
            'base_url': settings.get('base_url') or DEFAULT_ONLINE['base_url'],
            'has_key': bool(key), 'key_hint': _mask_key(key)}
    if not key:
        return {**base, 'available': False,
                'reason': '在线模型还没填 API Key。填一次就存在本机，不会上传。'}
    return {**base, 'available': True, 'reason': '',
            'hint': '走你自己配的在线接口；平台规则可能拒答成人向内容。'}


def capability() -> dict[str, Any]:
    """给界面判断两条通道能不能用：本地看服务/模型，在线看 Key。"""
    local = _local_state()
    online = _online_state()
    chosen = load_config()['provider']
    # 旧字段保留：顶层的 available/model/... 一律描述本地通道，避免旧调用方突然失效。
    return {
        **{key: local[key] for key in ('available', 'running', 'model', 'reason', 'can_start',
                                       'executable', 'installed')},
        'default_model': MODEL_ID,
        'service': SERVICE_URL,
        'models_root': str(_models_root()),
        'hint': local.get('hint') or '',
        'keep_alive': keep_alive_value(),
        'default': chosen,
        'providers': {'local': local, 'online': online},
    }


# ---------------------------------------------------------------- 生成

def _clean(text: str) -> str:
    """模型很爱自己加前缀和引号，这里统一剥掉；同时把多段压成一段。"""
    value = _THINK_BLOCK.sub('', str(text or '')).strip()
    value = _THINK_OPEN.sub('', value).strip()
    value = _LABEL.sub('', value).strip()
    value = re.sub(r'^[「『"\'\u201c\u2018]+|[」』"\'\u201d\u2019]+$', '', value).strip()
    value = re.sub(r'\s*\n+\s*', '', value)
    value = re.sub(r'[ \t]{2,}', ' ', value)
    return value[:MAX_OUTPUT]


def _image_roots() -> list[Path]:
    """识图只允许读这几棵树下的图片，别让它变成任意文件读取。"""
    from . import app as backend
    roots: list[Path] = []
    for name in ('DIRECTOR_WORKSPACES_ROOT', 'ROOT'):
        value = getattr(backend, name, None)
        if value:
            roots.append(Path(value))
    comfy = os.environ.get('DIRECTOR_COMFY_ROOT')
    if comfy:
        roots.append(Path(comfy) / 'ComfyUI_windows_portable' / 'ComfyUI' / 'output')
    # 素材库：识图最常吃的就是这些图（大多在 AI 参考图库）。不登记进来的话，
    # 写词里一勾「先让识图看一眼参考图」就会被「图片必须在作品目录内」挡掉。
    try:
        from . import reference_library
        config, _ = reference_library.load_config(Path(backend.ROOT))
        for item in (config.get('image_directories') or []):
            if isinstance(item, dict) and item.get('path'):
                roots.append(Path(str(item['path'])))
    except Exception:
        pass
    return roots


def resolve_image(path: str) -> Path:
    raw = str(path or '').strip()
    if not raw:
        raise ValueError('先选一张图')
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError('图片路径必须是绝对路径')
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('找不到这张图片') from exc
    if not resolved.is_file():
        raise ValueError('这不是一个文件')
    if resolved.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif'}:
        raise ValueError('只支持 PNG / JPG / WebP / HEIC')
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ValueError('读不到这张图') from exc
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f'图片太大（{size / 1024**2:.1f} MB，上限 16 MB）')
    roots = [root.resolve() for root in _image_roots() if root.exists()]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError('图片必须位于工作台的作品目录或数据目录内')
    return resolved


def _image_mime(path: Path) -> str:
    return {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp'}.get(path.suffix.lower(), 'image/png')


def _describe_online(target: Path, instruction: str | None = None) -> dict[str, Any]:
    """在线识图：OpenAI 兼容的 vision 消息格式（image_url + data URL）。"""
    settings = load_config()['online']
    key = online_key()
    if not key:
        raise WriterUnavailable('在线识图还没填 API Key：在提示词面板里填一次。')
    base_url = (settings.get('base_url') or DEFAULT_ONLINE['base_url']).rstrip('/')
    model = settings.get('model') or DEFAULT_ONLINE['model']
    vision_bytes, vision_mime = image_formats.read_for_vision(target)
    encoded = base64.b64encode(vision_bytes).decode('ascii')
    payload = {
        'model': model,
        'stream': False,
        'temperature': 0.4,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': instruction or DESCRIBE_INSTRUCTION},
                {'type': 'image_url',
                 'image_url': {'url': f'data:{vision_mime};base64,{encoded}'}},
            ],
        }],
    }
    started_at = time.monotonic()
    try:
        # 在线端点要走系统代理，这里不能 trust_env=False。
        response = httpx.post(f'{base_url}/chat/completions', json=payload,
                              headers=_online_headers(key), timeout=ONLINE_TIMEOUT)
    except httpx.ConnectError as exc:
        raise WriterUnavailable(f'连不上在线接口 {base_url}，检查地址和网络代理。') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'在线识图 {ONLINE_TIMEOUT:.0f} 秒没有回应。') from exc
    if response.status_code in (401, 403):
        raise WriterUnavailable('在线接口拒绝了这把 Key（401/403）。')
    if response.status_code in (400, 422):
        raise WriterFailed(f'{model} 可能不支持图片输入：{response.text[:200]}')
    if response.status_code >= 400:
        raise WriterFailed(f'{base_url} 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('在线接口返回了非 JSON 响应') from exc
    choices = body.get('choices') if isinstance(body, dict) else None
    message = (choices[0].get('message') if isinstance(choices, list) and choices else {}) or {}
    content = message.get('content')
    if isinstance(content, list):
        # 少数兼容实现会返回分段内容。
        content = ''.join(str(part.get('text') or '') for part in content if isinstance(part, dict))
    prompt = _clean(str(content or ''))
    if not prompt:
        raise WriterFailed('在线模型没有描述出内容，换一张图或换一个模型。')
    return {'prompt': prompt, 'provider': 'online', 'model': model, 'service': base_url,
            'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': False, 'startup_ms': 0}


def describe_image(path: str, *, provider: str = 'local',
                   instruction: str | None = None) -> dict[str, Any]:
    """图片 → 提示词。本地走 Ollama 多模态，在线走 OpenAI 兼容的 vision。

    ``instruction`` 留空时用「复现这张图」那套；只想要人物和构图（参考图补信息）
    时传 ``REFERENCE_DESCRIBE_INSTRUCTION``。
    """
    target = resolve_image(path)
    if provider == 'online':
        return _describe_online(target, instruction=instruction)
    if provider != 'local':
        raise ValueError('未知的识图通道')
    boot = ensure_service()
    model = active_model()
    # 实况图的主图是 HEIC：模型只吃 JPEG，这里转一次，别让调用方操心。
    vision_bytes, _ = image_formats.read_for_vision(target)
    encoded = base64.b64encode(vision_bytes).decode('ascii')
    payload = {
        'model': model,
        'stream': False,
        'think': False,
        'keep_alive': keep_alive_value(),
        'messages': [{'role': 'user',
                      'content': instruction or DESCRIBE_INSTRUCTION,
                      'images': [encoded]}],
        # Ollama 默认上下文只有 4096，一张没缩过的图就能顶爆（实测 4262 > 4096）。
        # 图已经缩到长边 1024，这里再把窗口抬到 8192 兜住素材标题较多的情况。
        'options': {'temperature': 0.4, 'top_p': 0.9, 'num_predict': 400,
                    'num_ctx': VISION_NUM_CTX},
    }
    started_at = time.monotonic()
    try:
        response = httpx.post(f'{SERVICE_URL}/api/chat', json=payload, timeout=TIMEOUT, trust_env=False)
    except httpx.ConnectError as exc:
        raise WriterUnavailable('连不上本机 Ollama，先启动服务再试') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'本机模型 {TIMEOUT:.0f} 秒没有回应') from exc
    if response.status_code == 400:
        detail = response.text[:300]
        # 400 不等于「不支持图片」：上下文溢出也是 400。分开报，别把人引到换模型的错路上。
        if 'context size' in detail or 'exceeds the available context' in detail:
            raise WriterFailed(
                f'图片太大，超出 {model} 的上下文窗口（已缩到长边 '
                f'{image_formats.VISION_MAX_EDGE} 仍不够）：{detail}')
        raise WriterFailed(f'模型 {model} 不接受图片输入，换一个带 vision 的模型：{detail}')
    if response.status_code == 404:
        raise WriterUnavailable(f'本机没有 {model}，执行：ollama pull {model}')
    if response.status_code >= 400:
        raise WriterFailed(f'Ollama 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('Ollama 返回了非 JSON 响应') from exc
    message = (body.get('message') or {}) if isinstance(body, dict) else {}
    prompt = _clean(str(message.get('content') or ''))
    if not prompt:
        raise WriterFailed('模型没有描述出内容，换一张图或再试一次')
    return {'prompt': prompt, 'provider': 'local', 'model': model, 'service': SERVICE_URL,
            'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': bool(boot.get('started')),
            'startup_ms': int(boot.get('wait_ms') or 0)}


def context_lines(context: Any) -> list[str]:
    """把工作台当前的选择翻成一句约束。

    只取「不用重画也说得清」的部分：素材槽的标题、画质档、画幅。素材本身不发给
    模型（它看不到图），发的是它叫什么——这已经足够让写出的描述对得上手里的图。
    """
    if not isinstance(context, dict):
        return []
    lines: list[str] = []
    materials = context.get('materials')
    if isinstance(materials, list):
        picked = []
        for entry in materials[:8]:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get('role') or '').strip()[:16]
            label = str(entry.get('label') or '').strip()[:60]
            if label:
                picked.append(f'{role}是「{label}」' if role else f'「{label}」')
        if picked:
            lines.append('已选定的参考素材（写成和它们一致的样子，不要另编长相和服装）：'
                         + '；'.join(picked) + '。')
    # 用户勾了「先看参考图」时，前端会先用识图把每张图变成一段文字，
    # 这里把它排在标题后面：标题只有商品名，描述才是画面里真正有什么。
    descriptions = context.get('reference_descriptions')
    if isinstance(descriptions, list):
        seen: list[str] = []
        for entry in descriptions[:3]:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get('role') or '').strip()[:16]
            text = str(entry.get('text') or '').strip()[:400]
            if text:
                seen.append(f'{role}：{text}' if role else text)
        if seen:
            lines.append('参考图里实际是什么（以这段为准，标题只是索引）：'
                         + '｜'.join(seen) + '。')
    aspect = str(context.get('aspect') or '').strip()[:16]
    if aspect:
        lines.append(f'画幅：{aspect}，取景要和它相称。')
    profile = str(context.get('profile') or '').strip()[:24]
    if profile:
        lines.append(f'风格档：{profile}。')
    return lines


def _messages(text: str, context: Any, mode: str = 'compose') -> list[dict[str, str]]:
    # 翻译是逐句对应，不带工作台上下文：说清"这段是待翻译的"就够了。
    if mode == 'translate':
        return [{'role': 'system', 'content': TRANSLATE_PROMPT},
                {'role': 'user', 'content': f'待翻译：{text}'}]
    extra = context_lines(context)
    label = '原文' if mode == 'polish' else '想法'
    user_message = text if not extra else '\n'.join([*extra, '', f'{label}：{text}'])
    system = POLISH_PROMPT if mode == 'polish' else SYSTEM_PROMPT
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user_message},
    ]


def _draft_local(text: str, *, temperature: float, num_predict: int,
                 context: Any, mode: str) -> dict[str, Any]:
    # 按需启动：工作台常驻时不碰 Ollama，用户点「生成」才在这里拉起。
    boot = ensure_service()
    model = active_model()
    payload = {
        'model': model,
        'stream': False,
        # qwen3 是思考模型：关掉思考，既快又不会把推理过程混进描述里。
        'think': False,
        'keep_alive': keep_alive_value(),
        'messages': _messages(text, context, mode),
        'options': {'temperature': temperature, 'top_p': 0.92, 'num_predict': num_predict},
    }
    started_at = time.monotonic()
    try:
        response = httpx.post(f'{SERVICE_URL}/api/chat', json=payload, timeout=TIMEOUT, trust_env=False)
    except httpx.ConnectError as exc:
        raise WriterUnavailable('连不上本机 Ollama（127.0.0.1:11434），先启动服务再试') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'本机模型 {TIMEOUT:.0f} 秒没有回应，可能是首次加载卡住了') from exc
    if response.status_code == 404:
        raise WriterUnavailable(f'本机没有 {model}，执行：ollama pull {model}')
    if response.status_code >= 400:
        raise WriterFailed(f'Ollama 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('Ollama 返回了非 JSON 响应') from exc
    message = (body.get('message') or {}) if isinstance(body, dict) else {}
    prompt = _clean(str(message.get('content') or ''))
    if not prompt:
        raise WriterFailed('模型返回了空内容，换个说法再试一次')
    return {'prompt': prompt, 'provider': 'local', 'model': model, 'service': SERVICE_URL,
            'mode': mode,
            'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': bool(boot.get('started')),
            'startup_ms': int(boot.get('wait_ms') or 0)}


def _draft_online(text: str, *, temperature: float, context: Any, mode: str) -> dict[str, Any]:
    settings = load_config()['online']
    key = online_key()
    if not key:
        raise WriterUnavailable('在线模型还没填 API Key：在面板里填一次，或设 DEEPSEEK_API_KEY。')
    base_url = (settings.get('base_url') or DEFAULT_ONLINE['base_url']).rstrip('/')
    model = settings.get('model') or DEFAULT_ONLINE['model']
    payload = {
        'model': model,
        'stream': False,
        'messages': _messages(text, context, mode),
        'temperature': max(0.0, min(2.0, float(temperature))),
    }
    started_at = time.monotonic()
    try:
        # 在线端点要走系统代理（如果配了），这里不能 trust_env=False。
        response = httpx.post(f'{base_url}/chat/completions', json=payload,
                              headers=_online_headers(key), timeout=ONLINE_TIMEOUT)
    except httpx.ConnectError as exc:
        raise WriterUnavailable(f'连不上在线接口 {base_url}，检查地址和网络代理。') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'在线模型 {ONLINE_TIMEOUT:.0f} 秒没有回应。') from exc
    if response.status_code in (401, 403):
        raise WriterUnavailable('在线模型拒绝了这把 Key（401/403），检查 Key 是否有效、有没有余额。')
    if response.status_code == 429:
        raise WriterFailed('在线模型限流了（429），等一会儿再试。')
    if response.status_code >= 400:
        raise WriterFailed(f'{base_url} 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('在线接口返回了非 JSON 响应') from exc
    choices = body.get('choices') if isinstance(body, dict) else None
    message = (choices[0].get('message') if isinstance(choices, list) and choices else {}) or {}
    prompt = _clean(str(message.get('content') or ''))
    if not prompt:
        raise WriterFailed('在线模型返回了空内容，换个说法再试一次')
    return {'prompt': prompt, 'provider': 'online', 'model': model, 'service': base_url,
            'mode': mode,
            'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': False, 'startup_ms': 0}


def draft(idea: str, *, provider: str = 'auto', temperature: float = 0.9,
          num_predict: int = 420, context: Any = None,
          mode: str = 'compose') -> dict[str, Any]:
    text = str(idea or '').strip()
    if not text:
        raise ValueError({'compose': '先写一句想法', 'polish': '先写一段要润色的提示词',
                          'translate': '先写一段要翻译的提示词'}.get(mode, '先写一句想法'))
    if len(text) > MAX_IDEA:
        raise ValueError(f'内容太长，控制在 {MAX_IDEA} 字以内')

    chosen = provider if provider in ('local', 'online') else load_config()['provider']
    shape = mode if mode in ('compose', 'polish', 'translate') else 'compose'
    # 翻译基本是 1:1 长度，长提示词按写词的 420 token 会被截断，给足量。
    budget = max(num_predict, 1200) if shape == 'translate' else num_predict
    if chosen == 'online':
        return _draft_online(text, temperature=temperature, context=context, mode=shape)
    return _draft_local(text, temperature=temperature, num_predict=budget,
                        context=context, mode=shape)


BREAKDOWN_MAX_CHARS = int(os.environ.get('DIRECTOR_PROMPT_BREAKDOWN_CHARS', '16000'))
BREAKDOWN_NUM_CTX = int(os.environ.get('DIRECTOR_PROMPT_BREAKDOWN_CTX', '16384'))

BREAKDOWN_PROMPT = """你是影视剧的制片统筹。读一段中文剧本，抽出真正需要单独准备的素材。
只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。

格式：
{"characters":[{"name":"角色名","look":"外观：年龄、性别、脸型、发型、体型、常穿服装","voice":"声线：年龄感、音色、语速、口音；没有就写空字符串"}],
 "scenes":[{"name":"地点","look":"时间、光线、环境细节、氛围"}],
 "props":[{"name":"道具","look":"外观、材质、状态"}],
 "music":[{"name":"段落名","style":"风格标签：乐器、节奏、情绪，无人声","seconds":30}]}

规则：
- 只收剧本里真的出现、且需要单独生成的；泛泛而谈的不要。
- 同一角色/场景/道具只出现一次，用剧本里的叫法。
- look 要具体可画；不要写景别、机位、运镜这类镜头语言。
- voice 只写声音本身，不要写台词内容。
- music 按情绪或段落给，最多 6 条；seconds 是建议时长（整数秒）。
- 没有的类别返回空数组。"""


def _breakdown_messages(text: str, system: str = BREAKDOWN_PROMPT) -> list[dict[str, str]]:
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': f'待处理的内容：\n{text}'}]


def _breakdown_local(text: str, *, temperature: float, num_predict: int,
                     system: str = BREAKDOWN_PROMPT) -> dict[str, Any]:
    boot = ensure_service()
    model = active_model()
    payload = {
        'model': model, 'stream': False, 'think': False,
        'keep_alive': keep_alive_value(),
        'messages': _breakdown_messages(text, system),
        # 剧本比一句想法长得多：默认 4096 上下文会直接截断，必须显式抬高。
        'options': {'temperature': temperature, 'top_p': 0.9,
                    'num_predict': num_predict, 'num_ctx': BREAKDOWN_NUM_CTX},
    }
    started_at = time.monotonic()
    try:
        response = httpx.post(f'{SERVICE_URL}/api/chat', json=payload, timeout=TIMEOUT, trust_env=False)
    except httpx.ConnectError as exc:
        raise WriterUnavailable('连不上本机 Ollama（127.0.0.1:11434），先启动服务再试') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'本机模型 {TIMEOUT:.0f} 秒没有回应，可能是首次加载卡住了') from exc
    if response.status_code == 404:
        raise WriterUnavailable(f'本机没有 {model}，执行：ollama pull {model}')
    if response.status_code >= 400:
        raise WriterFailed(f'Ollama 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('Ollama 返回了非 JSON 响应') from exc
    message = (body.get('message') or {}) if isinstance(body, dict) else {}
    content = str(message.get('content') or '')
    if not content.strip():
        raise WriterFailed('模型返回了空内容，再试一次')
    return {'text': content, 'provider': 'local', 'model': model,
            'service': SERVICE_URL, 'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': bool(boot.get('started')), 'startup_ms': int(boot.get('wait_ms') or 0)}


def _breakdown_online(text: str, *, temperature: float,
                      system: str = BREAKDOWN_PROMPT) -> dict[str, Any]:
    settings = load_config()['online']
    key = online_key()
    if not key:
        raise WriterUnavailable('在线模型还没填 API Key：在面板里填一次，或设 DEEPSEEK_API_KEY。')
    base_url = (settings.get('base_url') or DEFAULT_ONLINE['base_url']).rstrip('/')
    model = settings.get('model') or DEFAULT_ONLINE['model']
    payload = {'model': model, 'stream': False, 'messages': _breakdown_messages(text, system),
               'temperature': max(0.0, min(2.0, float(temperature)))}
    started_at = time.monotonic()
    try:
        response = httpx.post(f'{base_url}/chat/completions', json=payload,
                              headers=_online_headers(key), timeout=ONLINE_TIMEOUT)
    except httpx.ConnectError as exc:
        raise WriterUnavailable(f'连不上在线接口 {base_url}，检查地址和网络代理。') from exc
    except httpx.TimeoutException as exc:
        raise WriterFailed(f'在线模型 {ONLINE_TIMEOUT:.0f} 秒没有回应。') from exc
    if response.status_code in (401, 403):
        raise WriterUnavailable('在线模型拒绝了这把 Key（401/403），检查 Key 是否有效、有没有余额。')
    if response.status_code == 429:
        raise WriterFailed('在线模型限流了（429），等一会儿再试。')
    if response.status_code >= 400:
        raise WriterFailed(f'{base_url} 返回 {response.status_code}：{response.text[:200]}')
    try:
        body = response.json()
    except ValueError as exc:
        raise WriterFailed('在线接口返回了非 JSON 响应') from exc
    choices = body.get('choices') if isinstance(body, dict) else None
    message = (choices[0].get('message') if isinstance(choices, list) and choices else {}) or {}
    content = str(message.get('content') or '')
    if not content.strip():
        raise WriterFailed('在线模型返回了空内容，再试一次')
    return {'text': content, 'provider': 'online', 'model': model,
            'service': base_url, 'elapsed_ms': int((time.monotonic() - started_at) * 1000),
            'started_service': False, 'startup_ms': 0}


def json_chat(text: str, *, system: str, provider: str = 'auto',
              temperature: float = 0.2, num_predict: int = 2600) -> dict[str, Any]:
    """带自定义 system 的一次结构化对话：所有「读一段文字、吐一份 JSON」都走这里。

    每个调用方自己写清职责边界（见 contracts/prompt-reading-roles.md），
    这里只负责通道选择、长度截断和错误归因。
    """
    body = str(text or '').strip()
    if not body:
        raise ValueError('内容还是空的')
    truncated = len(body) > BREAKDOWN_MAX_CHARS
    if truncated:
        body = body[:BREAKDOWN_MAX_CHARS]
    chosen = provider if provider in ('local', 'online') else load_config()['provider']
    if chosen == 'online':
        result = _breakdown_online(body, temperature=temperature, system=system)
    else:
        result = _breakdown_local(body, temperature=temperature, num_predict=num_predict, system=system)
    result.update(truncated=truncated, source_chars=len(body))
    return result


def breakdown(text: str, *, provider: str = 'auto', temperature: float = 0.2,
              num_predict: int = 2600) -> dict[str, Any]:
    """把剧本正文拆成素材清单（结构化 JSON 文本，解析在 script_library 里做）。"""
    return json_chat(text, system=BREAKDOWN_PROMPT, provider=provider,
                     temperature=temperature, num_predict=num_predict)
