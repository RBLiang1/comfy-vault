#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H3 经验库  ——  ComfyUI 单轮经验收录器（不限模型 / 不限工作流）
初版名叫 H3-Vault（当时只跑 MiniMax H3），现在已与模型解耦，项目名用 comfy-vault。

设计原则
  1. 只读 ComfyUI 执行历史，绝不自动收录。用户觉得这一轮好，主动点「保留」，才落盘。
  2. 解析与具体工作流解耦：不写死节点 id、不写死 MiniMax H3 的字段名。
     提示词 / 种子 / 参考图 / 输出 / 分辨率 / 时长 / 耗时 都按「通用特征」去捞，
     换任何生图、生视频、放大、局部重绘工作流都能用。

启动：python app.py    然后浏览器打开 http://127.0.0.1:8791
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
CFG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "data" / "vault.sqlite3"

DEFAULT_CFG = {
    "comfy_url": "http://127.0.0.1:8188",
    "port": 8791,
    "vault_dir": "vault",
    "notes_dir": "notes",
    "pending_limit": 60,
    # ComfyUI 已保存工作流所在目录；留空则在启动时自动探测
    "comfy_workflows_dir": "",
}
CFG = dict(DEFAULT_CFG)

VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT | AUDIO_EXT

# 提示词可能出现的输入字段名（按可信度排序）——不同工作流用的是不同的键
TEXT_KEYS = ("prompt", "text", "text_g", "positive_prompt", "positive", "prompt_text",
             "string", "text_l", "instruction")
NEG_KEYS = ("negative_prompt", "negative", "text_l", "neg_prompt")

# 参考素材可能出现的输入字段名
REF_KEYS = ("first_frame", "last_frame", "image", "images", "video", "file", "audio",
            "input_image", "init_image", "mask", "reference_image")
REF_PATTERN = re.compile(r"^(ref_(image|video|audio)_\d+|image_\d+|video_\d+|source_image)$")

# 模型家族识别（按文件名关键字）
FAMILY_RULES = [
    (("minimax_h3", "minimaxh3", "minimax h3"), "MiniMax H3"),
    (("qwen_image", "qwen-image", "qwenimage"), "Qwen-Image"),
    (("flux",), "FLUX"),
    (("z_image", "zimage", "z-image"), "Z-Image"),
    (("moss",), "MOSS"),
    (("hidream", "hi-dream"), "HiDream"),
    (("kolors",), "Kolors"),
    (("sd3", "stable_diffusion_3", "stable-diffusion-3"), "SD3"),
    (("illustrious", "noobai", "noob", "pony", "animagine", "sdxl", "juggernaut",
      "dreamshaper", "realvis"), "SDXL"),
    (("wan2", "wan_2", "wanvideo"), "Wan"),
    (("hunyuan",), "Hunyuan"),
    (("ltx",), "LTX"),
    (("cogvideo",), "CogVideo"),
    (("sd_v1", "v1-5", "sd15"), "SD1.5"),
]

# 工作流特征包 → 中文别名（用于给工作流起名，方便区分）
PACK_HINTS = [
    ("Fisher", "姿态"),
    ("ControlNet", "ControlNet"),
    ("Upscale", "放大"),
    ("Inpaint", "局部重绘"),
    ("Detailer", "细节修复"),
    ("FaceDetailer", "脸部修复"),
    ("AnimateDiff", "动画"),
    ("IpAdapter", "IPAdapter"),
    ("InstantID", "InstantID"),
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def load_cfg() -> None:
    global CFG
    CFG = dict(DEFAULT_CFG)
    if CFG_PATH.exists():
        try:
            CFG.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"[warn] config.json 读取失败，使用默认配置: {exc}")
    else:
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    for key in ("vault_dir", "notes_dir"):
        vd = str(CFG.get(key) or DEFAULT_CFG[key])
        CFG[key] = vd if os.path.isabs(vd) else str((ROOT / vd).resolve())


load_cfg()
VAULT_DIR = Path(CFG["vault_dir"])
VAULT_DIR.mkdir(parents=True, exist_ok=True)
NOTES_DIR = Path(CFG["notes_dir"])
NOTES_DIR.mkdir(parents=True, exist_ok=True)
# 总结正文里粘贴的图片，按内容哈希存这里，正文只留 /na/<文件名> 引用
ASSETS_DIR = NOTES_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "data").mkdir(parents=True, exist_ok=True)


def comfy_url(path: str) -> str:
    return CFG["comfy_url"].rstrip("/") + path


# --------------------------------------------------------------------------
# 工作流全称索引
#   ComfyUI 执行历史里只有 workflow_id（一个 UUID），没有可读名字；
#   而用户「已保存的工作流」文件里带着同一个 id，文件名就是界面上看到的全称。
#   所以按 id 建一张索引，收录时把全称一并写进记录 —— 同名模型的不同工作流
#   才能区分开。
# --------------------------------------------------------------------------
WF_ID_RE = re.compile(r'"id"\s*:\s*"([0-9a-fA-F-]{16,64})"')
_WF_INDEX = {"dir": "", "at": 0.0, "by_id": {}, "by_short": {}}
_WF_TTL = 180.0


def _comfy_roots_from_process() -> list[str]:
    """从运行中的 python 进程命令行里找出 ComfyUI 的 main.py 所在目录。"""
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    roots = []
    for line in (proc.stdout or "").splitlines():
        m = re.search(r"([A-Za-z]:\\[^\"]*?)\\main\.py", line, re.I)
        if m:
            roots.append(m.group(1))
    return roots


def discover_workflows_dir() -> str:
    """定位 ComfyUI 存放已保存工作流的目录：先看配置，再从进程命令行推断。"""
    cands: list[str] = []
    cfg_dir = str(CFG.get("comfy_workflows_dir") or "").strip()
    if cfg_dir:
        cands.append(cfg_dir)
    env_dir = os.environ.get("COMFYUI_DIR", "").strip()
    if env_dir:
        cands.append(os.path.join(env_dir, "user", "default", "workflows"))
    for root in _comfy_roots_from_process():
        cands.append(os.path.join(root, "user", "default", "workflows"))
        cands.append(os.path.join(root, "user", "workflows"))
    for path in cands:
        if path and os.path.isdir(path):
            return os.path.abspath(path)
    return ""


def _read_workflow_id(path: str) -> str:
    """id 是工作流文件的第一个字段，只读头部即可，不必整文件解析。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192).decode("utf-8", "ignore")
        m = WF_ID_RE.search(head)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def workflow_index(force: bool = False) -> dict:
    """id -> {name, file, score}；同一 id 的 .bak 副本会被排到后面。"""
    now = time.time()
    if not force and _WF_INDEX["by_id"] and now - _WF_INDEX["at"] < _WF_TTL:
        return _WF_INDEX
    root = _WF_INDEX["dir"] or discover_workflows_dir()
    if not root or not os.path.isdir(root):
        _WF_INDEX.update({"dir": "", "at": now, "by_id": {}, "by_short": {}})
        return _WF_INDEX
    by_id: dict[str, dict] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(".json"):
                continue
            wid = _read_workflow_id(os.path.join(dirpath, fn))
            if not wid:
                continue
            name = fn[:-5]
            low = name.lower()
            score = -len(name) * 0.01
            if ".bak" in low or "_before_" in low or "备份" in name:
                score -= 100
            prev = by_id.get(wid)
            if prev and prev["score"] >= score:
                continue
            by_id[wid] = {
                "score": score,
                "name": name,
                "file": os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"),
            }
    by_short: dict[str, dict] = {}
    for wid, info in by_id.items():
        by_short.setdefault(wid[:12], info)
    _WF_INDEX.update({"dir": root, "at": now, "by_id": by_id, "by_short": by_short})
    return _WF_INDEX


def lookup_workflow(wid: str) -> dict:
    """按完整 id 或前 12 位查工作流全称；查不到返回空字典。"""
    wid = (wid or "").strip().lower()
    if not wid:
        return {}
    idx = workflow_index()
    hit = idx["by_id"].get(wid) or idx["by_short"].get(wid[:12])
    if not hit and time.time() - idx["at"] > 15:
        idx = workflow_index(force=True)
        hit = idx["by_id"].get(wid) or idx["by_short"].get(wid[:12])
    return hit or {}


def comfy_json(path: str, timeout: float = 20.0):
    with urllib.request.urlopen(comfy_url(path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def comfy_bytes(view_path: str, timeout: float = 300.0) -> bytes:
    with urllib.request.urlopen(comfy_url(view_path), timeout=timeout) as r:
        return r.read()


def to_num(v, cast=float):
    """ComfyUI 里数值经常以字符串形式出现（'20260930' / '25'），统一转数字。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return cast(v)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            try:
                return cast(float(s))
            except Exception:
                return None
    return None


def norm_ts(v):
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    v = int(v)
    return v * 1000 if v < 100_000_000_000 else v


def iso_local(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def kind_of(name: str) -> str:
    ext = Path(str(name)).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def safe_slug(text: str, limit: int = 36) -> str:
    text = re.sub(r"[\r\n\t]+", " ", (text or "").strip())
    text = re.sub(r"[\\/:*?\"<>|\s]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return text[:limit] or "run"


def image_size(path: str) -> tuple[int, int]:
    """不依赖 PIL，直接从文件头读图片尺寸（PNG / WebP / JPEG）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return (0, 0)
        with p.open("rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                if head[12:16] == b"VP8X":
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return w, h
                if head[12:16] == b"VP8 ":
                    w = int.from_bytes(head[26:28], "little") & 0x3FFF
                    h = int.from_bytes(head[28:30], "little") & 0x3FFF
                    return w, h
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        break
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        return int(w), int(h)
                    seg = f.read(2)
                    if len(seg) < 2:
                        break
                    f.seek(int.from_bytes(seg, "big") - 2, 1)
    except Exception:
        pass
    return (0, 0)


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id TEXT UNIQUE NOT NULL,
                dir_name TEXT NOT NULL,
                title TEXT DEFAULT '',
                prompt_text TEXT DEFAULT '',
                negative_text TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                note TEXT DEFAULT '',
                rating INTEGER DEFAULT 0,
                created_at TEXT DEFAULT '',
                kept_at TEXT DEFAULT '',
                elapsed_ms INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0,
                seed TEXT DEFAULT '',
                steps INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                flow_id TEXT DEFAULT '',
                flow TEXT DEFAULT '',
                media_file TEXT DEFAULT '',
                cover_file TEXT DEFAULT '',
                meta_json TEXT DEFAULT ''
            )"""
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        for col, ddl in (("flow_id", "TEXT DEFAULT ''"), ("flow", "TEXT DEFAULT ''"),
                         ("negative_text", "TEXT DEFAULT ''")):
            if col not in cols:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")


init_db()


# --------------------------------------------------------------------------
# 解析（工作流无关）
# --------------------------------------------------------------------------
def _iter_nodes(nodes: dict):
    for nid, node in nodes.items():
        if isinstance(node, dict):
            yield nid, node


LITERAL_KEYS = ("value", "int", "float", "string", "text", "seed", "steps", "number", "val")


def resolve_literal(nodes: dict, val, depth: int = 0):
    """ComfyUI 的参数常常不是字面量而是「连线」[上游节点id, 槽位]。
    这里顺着连线找到上游 Primitive 节点的真实值，最多跟 4 层。"""
    if depth > 4 or not isinstance(val, list) or len(val) != 2:
        return val
    nid = str(val[0])
    node = nodes.get(nid)
    if not isinstance(node, dict):
        return None
    ins = node.get("inputs") or {}
    for key in LITERAL_KEYS:
        if key in ins:
            v = ins[key]
            if isinstance(v, list):
                return resolve_literal(nodes, v, depth + 1)
            return v
    for v in ins.values():  # 兜底：任意非连线字面量
        if not isinstance(v, list):
            return v
    return None


NUMERIC_JUNK = re.compile(r"^[\s\d,.\-_xX*/+=%()]+$")

# 有些工作流会把 None / null 当成字符串写进文本字段，这些不是提示词
JUNK_LITERALS = {"none", "null", "undefined", "nil", "nan", "true", "false", "empty", "no prompt"}


def _is_meaningful_text(text: str) -> bool:
    """过滤掉纯数字、比例、随机种子、None 这类假提示词。"""
    if not text or len(text) < 4:
        return False
    if text.strip().lower() in JUNK_LITERALS:
        return False
    if NUMERIC_JUNK.match(text):
        return False
    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    return letters >= max(4, int(len(text) * 0.3))


def collect_texts(nodes: dict, outputs: dict) -> tuple[str, str, list[dict]]:
    """从节点输入 + 节点输出里捞提示词。

    不同工作流差异很大：
      - SD/SDXL：CLIPTextEncode.text
      - Flux/Qwen：CLIPTextEncode.text 或自定义节点的 prompt
      - MiniMax H3：单个 Integration 节点的 prompt
      - 姿态/风格类：提示词是上游节点「输出」的文本（如 fisher_prompt）
      - 参数由 Primitive 节点提供时，值是「连线」，需要追到上游
    按「字段名可信度 + 文本长度」打分选主提示词，其余存为附加文本。
    """
    cands: list[dict] = []

    for nid, node in _iter_nodes(nodes):
        ct = node.get("class_type") or ""
        ins = node.get("inputs") or {}
        for key, raw in ins.items():
            val = resolve_literal(nodes, raw) if isinstance(raw, list) else raw
            if not isinstance(val, str):
                continue
            text = val.strip()
            if not _is_meaningful_text(text):
                continue
            low = key.lower()
            neg = low in NEG_KEYS or "negative" in low
            if key in ("prompt", "prompt_text"):
                weight = 100
            elif key in TEXT_KEYS or low in TEXT_KEYS:
                weight = 90
            elif low in NEG_KEYS:
                weight = 60
            elif "text" in low or "prompt" in low:
                weight = 55
            elif key == "value" and any(t in ct.lower() for t in ("string", "text", "prompt")):
                weight = 50
            else:
                continue
            if "filename" in low or "name" in low:
                continue
            cands.append({"text": text, "score": weight + min(len(text), 3000),
                          "negative": neg, "node": nid, "class": ct, "field": key})


    # 节点输出里的文本（上游节点算出来的提示词）
    for nid, payload in (outputs or {}).items():
        if not isinstance(payload, dict):
            continue
        for slot, entries in payload.items():
            if not isinstance(entries, list):
                continue
            for ent in entries:
                if isinstance(ent, str):
                    texts = [ent]
                elif isinstance(ent, dict):
                    texts = [v for v in ent.values() if isinstance(v, str)]
                else:
                    continue
                for text in texts:
                    text = text.strip()
                    if not _is_meaningful_text(text):
                        continue
                    low = slot.lower()
                    weight = 85 if ("prompt" in low or "text" in low) else 45
                    cands.append({"text": text, "score": weight + min(len(text), 3000),
                                  "negative": "negative" in low or "neg_" in low,
                                  "node": nid, "class": "output", "field": slot})

    if not cands:
        return "", "", []

    cands.sort(key=lambda c: c["score"], reverse=True)
    positives = [c for c in cands if not c["negative"]]
    negatives = [c for c in cands if c["negative"]]
    prompt = positives[0]["text"] if positives else ""
    negative = negatives[0]["text"] if negatives else ""

    extras, seen = [], {prompt, negative}
    for c in cands:
        if c["text"] in seen:
            continue
        seen.add(c["text"])
        extras.append({"text": c["text"][:6000], "field": c["field"], "class": c["class"]})
        if len(extras) >= 4:
            break
    return prompt, negative, extras


def collect_refs(nodes: dict) -> list[dict]:
    """首帧 / 参考图 / 参考视频 / 遮罩，全部按「字段名 + 文件后缀」识别。"""
    refs, seen = [], set()
    role_order = {"first_frame": 0, "last_frame": 1, "init_image": 2, "reference_image": 3}

    def label_for(key: str) -> str:
        if key in role_order:
            return {"first_frame": "首帧", "last_frame": "尾帧", "init_image": "底图",
                    "reference_image": "参考图"}[key]
        m = re.match(r"^ref_(image|video|audio)_(\d+)$", key)
        if m:
            return {"image": "参考图 ", "video": "参考视频 ", "audio": "参考音频 "}[m.group(1)] + m.group(2)
        if key in ("image", "images", "input_image"):
            return "参考图"
        if key in ("video", "file"):
            return "参考视频"
        if key == "mask":
            return "遮罩"
        if key == "audio":
            return "参考音频"
        return key

    for nid, node in _iter_nodes(nodes):
        for key, val in (node.get("inputs") or {}).items():
            vals = val if isinstance(val, list) else [val]
            for one in vals:
                if not isinstance(one, str):
                    continue
                one = one.strip()
                if not one or one in seen:
                    continue
                if key not in REF_KEYS and not REF_PATTERN.match(key):
                    continue
                if Path(one).suffix.lower() not in MEDIA_EXT:
                    continue
                seen.add(one)
                refs.append({
                    "role": key, "label": label_for(key), "filename": one,
                    "kind": kind_of(one), "node": nid,
                    "url": "/api/view?" + urllib.parse.urlencode(
                        {"filename": one, "subfolder": "", "type": "input"}),
                })
    refs.sort(key=lambda r: (role_order.get(r["role"], 5), r["role"], r["filename"]))
    return refs


def collect_numbers(nodes: dict) -> dict:
    """种子 / 步数 / 采样器 / CFG——按字段名捞，兼容字符串数字与 Primitive 连线。"""
    out = {"seeds": [], "steps": 0, "sampler": {}}
    best_seed, best_rank = None, -1

    for nid, node in _iter_nodes(nodes):
        ct = (node.get("class_type") or "")
        ins = node.get("inputs") or {}

        def val_of(key):
            raw = ins.get(key)
            return resolve_literal(nodes, raw) if isinstance(raw, list) else raw

        # 种子优先取「采样器 / 噪声」节点的，避免抓到辅助节点的随机数
        rank = 2 if any(t in ct for t in ("KSampler", "SamplerCustom", "RandomNoise",
                                          "Sampler", "Noise")) else 0
        for key in ("seed", "noise_seed", "rand_seed"):
            n = to_num(val_of(key), int)
            if n is None:
                continue
            if n not in out["seeds"]:
                out["seeds"].append(n)
            if rank > best_rank:
                best_seed, best_rank = n, rank

        for key, cast in (("steps", int), ("cfg", float), ("denoise", float),
                          ("shift_video", float), ("shift_audio", float), ("step", int)):
            if key in ins:
                n = to_num(val_of(key), cast)
                if n is not None:
                    target = "steps" if key == "step" else key
                    out["sampler"].setdefault(target, n)
        for key in ("sampler_name", "scheduler"):
            v = val_of(key)
            if isinstance(v, str) and key not in out["sampler"]:
                out["sampler"][key] = v

    if best_seed is not None and out["seeds"] and out["seeds"][0] != best_seed:
        out["seeds"].remove(best_seed)
        out["seeds"].insert(0, best_seed)
    out["steps"] = int(out["sampler"].get("steps") or 0)
    return out


def collect_models(nodes: dict) -> dict:
    unet, lora, clip, vae, ckpt = [], [], [], [], []
    for nid, node in _iter_nodes(nodes):
        ct = node.get("class_type") or ""
        ins = node.get("inputs") or {}
        for key, bucket in (("unet_name", unet), ("ckpt_name", ckpt), ("checkpoint", ckpt),
                            ("clip_name", clip), ("vae_name", vae),
                            ("video_vae_name", vae), ("audio_vae_name", vae)):
            v = ins.get(key)
            if isinstance(v, str) and v and v not in bucket:
                bucket.append(v)
        if isinstance(ins.get("lora_name"), str) and ins["lora_name"]:
            item = {"name": ins["lora_name"],
                    "strength": to_num(ins.get("strength_model"), float) or 1.0}
            if all(l["name"] != item["name"] for l in lora):
                lora.append(item)
    return {"unet": unet, "ckpt": ckpt, "lora": lora, "clip": clip, "vae": vae}


def collect_outputs(rec: dict) -> list[dict]:
    outs = []
    for node_id, payload in (rec.get("outputs") or {}).items():
        if not isinstance(payload, dict):
            continue
        for slot, entries in payload.items():
            if not isinstance(entries, list):
                continue
            for ent in entries:
                if not isinstance(ent, dict) or not ent.get("filename"):
                    continue
                meta = ent.get("metadata") or {}
                name = ent["filename"]
                kind = kind_of(name)
                if kind == "other":
                    continue
                w = int(to_num(meta.get("width"), int) or 0)
                h = int(to_num(meta.get("height"), int) or 0)
                if (not w or not h) and ent.get("fullpath"):
                    w2, h2 = image_size(ent["fullpath"])
                    w, h = w or w2, h or h2
                fc = int(to_num(meta.get("frame_count"), int) or 0)
                dummy = bool(w and h and w <= 128 and h <= 128 and fc <= 1)
                sub, typ = ent.get("subfolder") or "", ent.get("type") or "output"
                outs.append({
                    "node": node_id, "slot": slot, "filename": name, "subfolder": sub,
                    "type": typ, "kind": kind, "width": w, "height": h,
                    "duration": round(float(to_num(meta.get("duration"), float) or 0), 3),
                    "fps": to_num(meta.get("fps"), float) or to_num(ent.get("frame_rate"), float) or 0,
                    "frame_count": fc, "format": ent.get("format") or "",
                    "fullpath": ent.get("fullpath") or "", "dummy": dummy,
                    "url": "/api/view?" + urllib.parse.urlencode(
                        {"filename": name, "subfolder": sub, "type": typ}),
                })
    real = [o for o in outs if not o["dummy"]]
    real.sort(key=lambda o: (o["type"] == "output",
                             o["width"] * o["height"] * max(1, o["frame_count"])), reverse=True)
    for o in outs:
        o["primary"] = bool(real) and o is real[0]
    outs.sort(key=lambda o: (not o["primary"], o["dummy"], -o["width"] * o["height"]))
    return outs


LABEL_LINE = re.compile(r"^[a-zA-Z_ ]{3,48}:?\s*$")


def guess_title(prompt_text: str) -> str:
    """从提示词里挑一行当默认标题，跳过 subject_definitions: 这类标签行。"""
    if not prompt_text:
        return ""

    def pick(text: str) -> str:
        text = text.strip().strip(" -·#")
        if not text:
            return ""
        if LABEL_LINE.match(text) or len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text)) < 6:
            return ""
        return text[:36]

    m = re.search(r"summary:\s*\n+([^\n]+)", prompt_text)
    if m:
        t = pick(m.group(1))
        if t:
            return t
    for line in prompt_text.splitlines():
        t = pick(line)
        if t:
            return t
    fallback = prompt_text.strip().replace("\n", " ")
    if fallback.lower() in JUNK_LITERALS:
        return ""
    return fallback[:36]


def model_family(models: dict, class_names: set[str]) -> str:
    if any("MiniMaxH3" in c or "H3Integration" in c for c in class_names):
        return "MiniMax H3"
    blob = " ".join(models.get("unet", []) + models.get("ckpt", []) + models.get("clip", [])).lower()
    for keys, name in FAMILY_RULES:
        if any(k in blob for k in keys):
            return name
    return "自定义模型" if blob else "未知模型"


def flow_signature(nodes: dict, extra: dict, models: dict, class_names: set[str]) -> dict:
    """给工作流一个稳定身份：同一个工作流每次跑都能归到一起。"""
    wm = extra.get("workflow_metadata") or {}
    wid = wm.get("workflow_id") if isinstance(wm, dict) else None
    if not wid:
        wf = ((extra.get("extra_pnginfo") or {}).get("workflow") or {})
        wid = wf.get("id") if isinstance(wf, dict) else None
    if not wid:
        blob = ",".join(sorted(c for c in class_names if c))
        wid = "sig_" + hashlib.md5(blob.encode("utf-8")).hexdigest()[:8]

    family = model_family(models, class_names)
    hints = []
    for key, label in PACK_HINTS:
        if any(key.lower() in c.lower() for c in class_names) and label not in hints:
            hints.append(label)
    derived = family + ("".join(" · " + h for h in hints[:2]) if hints else "")

    # 有「已保存工作流」能对上号就用它的全称，否则退回按模型推断的名字
    info = lookup_workflow(str(wid))
    return {
        "id": str(wid)[:12],
        "name": info["name"] if info else derived,
        "family": family,
        "hints": hints,
        "derived_name": derived,
        "workflow_id": str(wid),
        "workflow_file": info.get("file", ""),
    }


def parse_run(pid: str, rec: dict, index: int = 0) -> dict:
    pr = rec.get("prompt") or []
    nodes = pr[2] if len(pr) > 2 and isinstance(pr[2], dict) else {}
    extra = pr[3] if len(pr) > 3 and isinstance(pr[3], dict) else {}
    st = rec.get("status") or {}
    msgs = st.get("messages") or []

    start = end = None
    for m in msgs:
        if not isinstance(m, (list, tuple)) or len(m) < 2:
            continue
        name, data = m[0], (m[1] if isinstance(m[1], dict) else {})
        ts = norm_ts(data.get("timestamp"))
        if name == "execution_start" and ts:
            start = ts
        elif name in ("execution_success", "execution_error") and ts:
            end = ts

    created = norm_ts(extra.get("create_time")) or start
    status = st.get("status_str") or ("success" if st.get("completed") else "unknown")

    prompt_text, negative_text, extra_texts = collect_texts(nodes, rec.get("outputs") or {})
    outputs = collect_outputs(rec)
    primary = next((o for o in outputs if o["primary"]), None)
    refs = collect_refs(nodes)
    nums = collect_numbers(nodes)
    models = collect_models(nodes)
    class_names = {n.get("class_type") for n in nodes.values() if n.get("class_type")}
    flow = flow_signature(nodes, extra, models, class_names)

    width = (primary or {}).get("width") or 0
    height = (primary or {}).get("height") or 0
    duration = (primary or {}).get("duration") or 0
    if not duration:
        for node in nodes.values():
            ins = node.get("inputs") or {}
            d = to_num(ins.get("duration_seconds") or ins.get("duration"), float)
            if d:
                duration = d
                break

    return {
        "prompt_id": pid,
        "index": index,
        "status": status,
        "completed": bool(st.get("completed")),
        "created_at": iso_local(created),
        "created_ms": created or 0,
        "finished_at": iso_local(end),
        "elapsed_ms": (end - start) if (start and end) else 0,
        "title_suggest": guess_title(prompt_text),
        "prompt_text": prompt_text,
        "negative_text": negative_text,
        "extra_texts": extra_texts,
        "seed": nums["seeds"][0] if nums["seeds"] else None,
        "seeds": nums["seeds"],
        "steps": nums["steps"],
        "sampler": nums["sampler"],
        "width": width,
        "height": height,
        "duration_s": round(duration, 3),
        "fps": (primary or {}).get("fps") or 0,
        "frame_count": (primary or {}).get("frame_count") or 0,
        "media_count": sum(1 for o in outputs if not o["dummy"]),
        "refs": refs,
        "outputs": outputs,
        "media_url": (primary or {}).get("url") or (outputs[0]["url"] if outputs else ""),
        "media_kind": (primary or {}).get("kind") or "",
        "models": models,
        "flow": flow,
        "node_count": len(nodes),
        "class_names": sorted(c for c in class_names if c),
    }


def fetch_history(limit: int) -> list[dict]:
    raw = comfy_json(f"/history?max_items={int(limit)}")
    runs = []
    for i, (pid, rec) in enumerate(raw.items()):
        try:
            runs.append(parse_run(pid, rec, i))
        except Exception:
            traceback.print_exc()
    runs.sort(key=lambda r: r["created_ms"], reverse=True)
    return runs


# --------------------------------------------------------------------------
# 收录
# --------------------------------------------------------------------------
def kept_prompt_ids() -> set[str]:
    with db() as conn:
        return {r["prompt_id"] for r in conn.execute("SELECT prompt_id FROM items")}


def unique_dir(name: str) -> Path:
    path, i = VAULT_DIR / name, 2
    while path.exists():
        path = VAULT_DIR / f"{name}-{i}"
        i += 1
    return path


def keep_run(pid: str, title: str = "", tags: str = "", note: str = "") -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE prompt_id=?", (pid,)).fetchone()
    if row:
        return {"ok": False, "error": "这一轮已经收录过了", "id": row["id"]}

    raw = comfy_json(f"/history/{urllib.parse.quote(pid)}")
    if pid not in raw:
        raise RuntimeError("ComfyUI 历史里找不到这一轮（可能已被清理）")
    rec = raw[pid]
    data = parse_run(pid, rec)

    # 只收正式输出；没有正式输出才退回临时文件（VHS 二采预览之类）
    media = [o for o in data["outputs"] if not o["dummy"]]
    official = [o for o in media if o["type"] == "output"] or media
    if not official:
        raise RuntimeError("这一轮没有可保存的成品文件")
    primary = next((o for o in official if o["primary"]), official[0])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_title = (title or data["title_suggest"] or "未命名").strip()
    dir_path = unique_dir(f"{stamp}_{safe_slug(final_title)}")
    dir_path.mkdir(parents=True, exist_ok=True)

    saved, media_file, cover_file = [], "", ""
    for o in official:
        try:
            blob = comfy_bytes("/view?" + urllib.parse.urlencode(
                {"filename": o["filename"], "subfolder": o["subfolder"], "type": o["type"]}))
        except Exception as exc:
            print(f"[warn] 拉取 {o['filename']} 失败: {exc}")
            continue
        target = dir_path / Path(o["filename"]).name
        if target.exists():
            target = dir_path / f"{o['node']}_{Path(o['filename']).name}"
        target.write_bytes(blob)
        saved.append(dict(o, stored=target.name, size=target.stat().st_size))
        if o["filename"] == primary["filename"]:
            media_file = target.name
        if not cover_file and o["kind"] == "image":
            cover_file = target.name
    if not media_file and saved:
        media_file = saved[0]["stored"]

    saved_refs = []
    for i, ref in enumerate(data["refs"]):
        try:
            blob = comfy_bytes("/view?" + urllib.parse.urlencode(
                {"filename": ref["filename"], "subfolder": "", "type": "input"}))
        except Exception as exc:
            print(f"[warn] 拉取参考图 {ref['filename']} 失败: {exc}")
            saved_refs.append(dict(ref, stored=""))
            continue
        target = dir_path / f"ref{i + 1}_{Path(ref['filename']).name}"
        target.write_bytes(blob)
        saved_refs.append(dict(ref, stored=target.name))

    workflow_nodes = rec.get("prompt", [])[2] if len(rec.get("prompt", [])) > 2 else {}
    (dir_path / "workflow_api.json").write_text(
        json.dumps(workflow_nodes, ensure_ascii=False, indent=2), encoding="utf-8")

    kept_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "schema": 2,
        "title": final_title,
        "prompt_id": pid,
        "kept_at": kept_at,
        "created_at": data["created_at"],
        "elapsed_ms": data["elapsed_ms"],
        "tags": [t.strip() for t in re.split(r"[,，\s]+", tags or "") if t.strip()],
        "note": note or "",
        "flow": data["flow"],
        "prompt_text": data["prompt_text"],
        "negative_text": data["negative_text"],
        "extra_texts": data["extra_texts"],
        "seed": data["seed"],
        "seeds": data["seeds"],
        "steps": data["steps"],
        "sampler": data["sampler"],
        "width": data["width"],
        "height": data["height"],
        "duration_s": data["duration_s"],
        "fps": data["fps"],
        "frame_count": data["frame_count"],
        "models": data["models"],
        "refs": saved_refs,
        "media": saved,
        "media_file": media_file,
        "cover_file": cover_file,
        "node_count": data["node_count"],
        "class_names": data["class_names"],
    }
    (dir_path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO items (prompt_id, dir_name, title, prompt_text, negative_text, tags,
                note, rating, created_at, kept_at, elapsed_ms, width, height, duration_s, seed,
                steps, model, flow_id, flow, media_file, cover_file, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, dir_path.name, final_title, data["prompt_text"], data["negative_text"],
             ",".join(meta["tags"]), meta["note"], 0, data["created_at"], kept_at,
             data["elapsed_ms"], data["width"], data["height"], data["duration_s"],
             str(data["seed"] if data["seed"] is not None else ""), data["steps"],
             (data["models"]["unet"] or data["models"]["ckpt"] or [""])[0],
             data["flow"]["id"], data["flow"]["name"], media_file, cover_file,
             json.dumps(meta, ensure_ascii=False)),
        )
        item_id = cur.lastrowid
    return {"ok": True, "id": item_id, "dir": str(dir_path), "meta": meta}


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def item_to_dict(row) -> dict:
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        meta = {}
    return {
        "id": row["id"], "prompt_id": row["prompt_id"], "dir_name": row["dir_name"],
        "title": row["title"], "prompt_text": row["prompt_text"],
        "negative_text": row["negative_text"] if "negative_text" in row.keys() else "",
        "tags": [t for t in (row["tags"] or "").split(",") if t], "note": row["note"],
        "rating": row["rating"], "created_at": row["created_at"], "kept_at": row["kept_at"],
        "elapsed_ms": row["elapsed_ms"], "width": row["width"], "height": row["height"],
        "duration_s": row["duration_s"], "seed": row["seed"], "steps": row["steps"],
        "model": row["model"],
        "flow_id": row["flow_id"] if "flow_id" in row.keys() else "",
        "flow": row["flow"] if "flow" in row.keys() else "",
        "media_file": row["media_file"], "cover_file": row["cover_file"],
        "media_url": f"/f/{row['id']}/{urllib.parse.quote(row['media_file'])}" if row["media_file"] else "",
        "cover_url": f"/f/{row['id']}/{urllib.parse.quote(row['cover_file'])}" if row["cover_file"] else "",
        "media_kind": kind_of(row["media_file"]),
        "meta": meta,
        "refs": [{"role": r.get("role"), "label": r.get("label"), "kind": r.get("kind"),
                  "filename": r.get("filename"),
                  "url": f"/f/{row['id']}/{urllib.parse.quote(r['stored'])}" if r.get("stored") else ""}
                 for r in meta.get("refs", [])],
        "files": [{"name": m.get("stored"), "kind": m.get("kind"), "size": m.get("size", 0),
                   "url": f"/f/{row['id']}/{urllib.parse.quote(m['stored'])}"}
                  for m in meta.get("media", []) if m.get("stored")],
    }


def list_items(q: str = "", tag: str = "", flow: str = "") -> list[dict]:
    sql, where, args = "SELECT * FROM items", [], []
    if q:
        where.append("(title LIKE ? OR prompt_text LIKE ? OR note LIKE ? OR tags LIKE ?"
                     " OR seed LIKE ? OR flow LIKE ?)")
        args += [f"%{q}%"] * 6
    if tag:
        where.append("(',' || tags || ',') LIKE ?")
        args.append(f"%,{tag},%")
    if flow:
        where.append("flow_id=?")
        args.append(flow)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    with db() as conn:
        return [item_to_dict(r) for r in conn.execute(sql, args)]


# --------------------------------------------------------------------------
# 经验总结（笔记）
# --------------------------------------------------------------------------
# 每篇总结 = notes/<id>.json。正文用轻量 markdown，案例引用写 [[12]] 或 [[12|说明]]，
# 12 是经验库条目的 id。这样「总体经验」和「典型案例」可以在同一篇里共存，
# 案例引用不复制内容，条目信息（标题/参数/封面）始终跟着经验库最新状态走。
REF_IN_TEXT = re.compile(r"\[\[\s*(\d+)\s*(?:\|([^\]]*))?\]\]")


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def extract_refs(content: str) -> list[int]:
    out: list[int] = []
    for m in REF_IN_TEXT.finditer(content or ""):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


def _note_files() -> list[Path]:
    return sorted(p for p in NOTES_DIR.glob("*.json") if p.is_file())


def load_note(nid: int) -> dict | None:
    p = NOTES_DIR / f"{nid}.json"
    if not p.exists():
        return None
    try:
        n = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    n["id"] = nid
    n.setdefault("title", "")
    n.setdefault("content", "")
    n.setdefault("tags", [])
    n.setdefault("created_at", "")
    n.setdefault("updated_at", "")
    return n


def save_note(note: dict) -> dict:
    note["refs"] = extract_refs(note.get("content", ""))
    note["updated_at"] = now_str()
    p = NOTES_DIR / f"{note['id']}.json"
    p.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    return note


# --------------------------------------------------------------------------
# 总结正文里的图片
#   粘贴进来的截图先落到 notes/assets/，正文里只写 ![](/na/<文件> "w=420")。
#   这样正文仍然是纯文本（可搜索、可导出），图片又不会塞进 JSON 把文件撑大。
# --------------------------------------------------------------------------
NOTE_IMG_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
}
NOTE_IMG_MAX = 24 * 1024 * 1024          # 单张 24MB 上限


def save_note_asset(data: bytes, ctype: str) -> dict:
    """按内容哈希落盘，同一张图重复粘贴不会存两份。"""
    ext = NOTE_IMG_EXT.get((ctype or "").split(";")[0].strip().lower(), ".png")
    name = hashlib.sha1(data).hexdigest()[:16] + ext
    path = ASSETS_DIR / name
    if not path.exists():
        path.write_bytes(data)
    w, h = image_size(str(path))
    return {"ok": True, "url": f"/na/{name}", "file": name,
            "width": w, "height": h, "bytes": len(data)}


def note_asset_path(name: str) -> Path | None:
    """只允许 assets 目录下的单层文件名，挡住路径穿越。"""
    name = urllib.parse.unquote(name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    target = (ASSETS_DIR / name).resolve()
    if target.parent != ASSETS_DIR.resolve() or not target.is_file():
        return None
    return target


def next_note_id() -> int:
    ids = []
    for p in _note_files():
        try:
            ids.append(int(p.stem))
        except ValueError:
            pass
    return (max(ids) + 1) if ids else 1


def note_summary(n: dict) -> dict:
    content = n.get("content") or ""
    plain = REF_IN_TEXT.sub(" ", content)
    plain = re.sub(r"[#>*`\-]{1,}", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return {"id": n["id"], "title": n.get("title") or "", "tags": n.get("tags") or [],
            "created_at": n.get("created_at") or "", "updated_at": n.get("updated_at") or "",
            "refs": extract_refs(content), "excerpt": plain[:90],
            "chars": len(content)}


def list_notes(q: str = "") -> list[dict]:
    notes = []
    for p in _note_files():
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            n["id"] = int(p.stem)
        except ValueError:
            continue
        notes.append(note_summary(n))
    if q:
        low = q.lower()
        notes = [n for n in notes if low in (n["title"] or "").lower()
                 or low in (n["excerpt"] or "").lower()
                 or any(low in str(t).lower() for t in n["tags"])]
    notes.sort(key=lambda n: n["updated_at"] or "", reverse=True)
    return notes


def note_as_markdown(n: dict, items: dict[int, dict]) -> str:
    """导出成 .md：把 [[12]] 换成带标题的小节，方便贴到别处。"""
    def repl(m):
        nid = int(m.group(1))
        it = items.get(nid)
        label = (m.group(2) or "").strip()
        if not it:
            return f"*（案例 #{nid} 已移出经验库）*"
        bits = [f"案例 #{nid} · {it['title'] or '(未命名)'}", f"工作流：{it['flow'] or '-'}"]
        if it["width"]:
            bits.append(f"{it['width']}×{it['height']}")
        if it["duration_s"]:
            bits.append(f"{it['duration_s']}s")
        if it["seed"]:
            bits.append(f"seed {it['seed']}")
        head = f"**{label + '：' if label else ''}{bits[0]}**（{'　'.join(bits[1:])}）"
        if it["prompt_text"]:
            head += f"\n\n> {it['prompt_text'][:400]}"
        return head
    body = REF_IN_TEXT.sub(repl, n.get("content") or "")
    # 正文里的图片是 /na/<文件名>，导出后改成相对路径，放到 notes/ 同级就能看到图
    body = re.sub(r"\]\(/na/([^)\s\"]+)", r"](assets/\1", body)
    title = n.get("title") or f"经验总结 {n['id']}"
    return f"# {title}\n\n_更新于 {n.get('updated_at') or '-'}_\n\n{body}\n"


def item_map() -> dict[int, dict]:
    with db() as conn:
        return {r["id"]: item_to_dict(r) for r in conn.execute("SELECT * FROM items")}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ComfyVault/2.0"

    def log_message(self, fmt, *args):
        if "/api/status" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS"),
                     ("Access-Control-Allow-Headers", "Content-Type")):
            self.send_header(k, v)
        self.end_headers()

    def do_HEAD(self):
        """只回头部：某些预览器 / 探测工具会先发 HEAD 判断服务是否活着。"""
        try:
            path = urllib.parse.urlparse(self.path).path
            ok = path in ("/", "/index.html", "/api/status")
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            if path == "/api/status":
                return self.api_status()
            if path == "/api/pending":
                return self.api_pending(qs)
            if path == "/api/vault":
                return self.api_vault(qs)
            if path == "/api/notes":
                return self.api_notes(qs)
            if path.startswith("/api/note/"):
                return self.api_note(path.rsplit("/", 1)[1], qs)
            if path.startswith("/api/item/"):
                return self.api_item(int(path.rsplit("/", 1)[1]))
            if path == "/api/view":
                return self.proxy_view(qs)
            if path.startswith("/f/"):
                return self.serve_vault_file(path)
            if path.startswith("/na/"):
                return self.serve_note_asset(path)
            return self.serve_static(path)
        except Exception as exc:
            traceback.print_exc()
            self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            # 图片上传是原始字节流，必须在按 JSON 解析 body 之前拦下来
            if path == "/api/note-asset":
                return self.api_note_asset()
            body = self._body()
            if path == "/api/keep":
                pid = (body.get("prompt_id") or "").strip()
                if not pid:
                    return self._err("缺少 prompt_id")
                return self._json(keep_run(pid, body.get("title", ""), body.get("tags", ""),
                                           body.get("note", "")))
            if path == "/api/keep-last":
                runs = [r for r in fetch_history(40) if r["status"] == "success" and r["media_url"]]
                if not runs:
                    return self._err("还没有跑成功的记录")
                return self._json(keep_run(runs[0]["prompt_id"], body.get("title", ""),
                                           body.get("tags", ""), body.get("note", "")))
            if path == "/api/reindex":
                return self._json({"ok": True, **reindex()})
            if path == "/api/notes":
                title = (body.get("title") or "").strip()
                note = {"id": next_note_id(), "title": title,
                        "content": body.get("content") or "",
                        "tags": body.get("tags") or [], "created_at": now_str()}
                note = save_note(note)
                return self._json({"ok": True, "note": note})
            return self._err("未知接口", 404)
        except Exception as exc:
            traceback.print_exc()
            return self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_PATCH(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/api/item/"):
                return self._err("未知接口", 404)
            item_id = int(path.rsplit("/", 1)[1])
            body = self._body()
            fields, args = [], []
            for key in ("title", "note"):
                if key in body:
                    fields.append(f"{key}=?")
                    args.append(str(body[key]))
            if "tags" in body:
                tags = body["tags"]
                tags = ",".join(t.strip() for t in tags if str(t).strip()) if isinstance(tags, list) else str(tags)
                fields.append("tags=?")
                args.append(tags)
            if "rating" in body:
                fields.append("rating=?")
                args.append(int(body["rating"]))
            if not fields:
                return self._err("没有可更新的字段")
            args.append(item_id)
            with db() as conn:
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not row:
                    return self._err("记录不存在", 404)
                conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", args)
                self.sync_meta(row, {"title": body.get("title"), "note": body.get("note"),
                                     "tags": body.get("tags")})
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return self._json({"ok": True, "item": item_to_dict(row)})
        except Exception as exc:
            traceback.print_exc()
            return self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_PUT(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/api/note/"):
                return self._err("未知接口", 404)
            nid = int(path.rsplit("/", 1)[1])
            note = load_note(nid)
            if not note:
                return self._err("这篇总结不存在", 404)
            body = self._body()
            for key in ("title", "content"):
                if key in body:
                    note[key] = str(body[key])
            if "tags" in body:
                tags = body["tags"]
                if isinstance(tags, str):
                    tags = [t.strip() for t in re.split(r"[,，\s]+", tags) if t.strip()]
                note["tags"] = [str(t).strip() for t in tags if str(t).strip()]
            note = save_note(note)
            return self._json({"ok": True, "note": note})
        except Exception as exc:
            traceback.print_exc()
            return self._err(f"{type(exc).__name__}: {exc}", 500)

    def do_DELETE(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/note/"):
                nid = int(path.rsplit("/", 1)[1])
                src = NOTES_DIR / f"{nid}.json"
                if not src.exists():
                    return self._err("这篇总结不存在", 404)
                trash = NOTES_DIR / "_trash"
                trash.mkdir(exist_ok=True)
                shutil.move(str(src), str(trash / f"{nid}.json"))
                return self._json({"ok": True, "moved_to": "notes/_trash"})
            if not path.startswith("/api/item/"):
                return self._err("未知接口", 404)
            item_id = int(path.rsplit("/", 1)[1])
            with db() as conn:
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not row:
                    return self._err("记录不存在", 404)
                src = VAULT_DIR / row["dir_name"]
                if src.exists():
                    trash = VAULT_DIR / "_trash"
                    trash.mkdir(exist_ok=True)
                    dest, i = trash / row["dir_name"], 2
                    while dest.exists():
                        dest = trash / f"{row['dir_name']}-{i}"
                        i += 1
                    shutil.move(str(src), str(dest))
                conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            return self._json({"ok": True, "moved_to": "_trash"})
        except Exception as exc:
            traceback.print_exc()
            return self._err(f"{type(exc).__name__}: {exc}", 500)

    # ---- 实现 ----
    def api_status(self):
        info = {"ok": True, "comfy": False, "comfy_url": CFG["comfy_url"],
                "vault_dir": str(VAULT_DIR), "count": 0, "version": ""}
        try:
            stats = comfy_json("/system_stats", timeout=6)
            info["comfy"] = True
            info["version"] = (stats.get("system") or {}).get("comfyui_version", "")
        except Exception as exc:
            info["comfy_error"] = f"{type(exc).__name__}: {exc}"
        with db() as conn:
            info["count"] = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        info["note_count"] = len(list_notes())
        info["notes_dir"] = str(NOTES_DIR)
        idx = workflow_index()
        info["workflow_dir"] = idx["dir"]
        info["workflow_count"] = len(idx["by_id"])
        self._json(info)

    def api_pending(self, qs):
        limit = int((qs.get("limit") or [CFG["pending_limit"]])[0])
        try:
            # 空跑（点了运行但队列没真正产出）在 ComfyUI 历史里也算一条记录，
            # 但它没有成品可收，混进待筛选只会变成一排「没有可预览的成品」占位卡。
            # 多拉一些再过滤，保证过滤后仍够 limit 条。
            runs = [r for r in fetch_history(max(limit * 2, limit + 20)) if r.get("media_url")]
            runs = runs[:limit]
        except Exception as exc:
            return self._json({"ok": False, "comfy": False, "runs": [],
                               "error": f"连不上 ComfyUI ({CFG['comfy_url']}): {exc}"})
        kept = kept_prompt_ids()
        # 同一个工作流在不同轮次里，被 bypass 的节点会让特征名抖动，
        # 这里按 flow_id 取最常见的名字，保证下拉里每个工作流只出现一次。
        name_votes: dict[str, dict[str, int]] = {}
        for r in runs:
            f = r["flow"]
            name_votes.setdefault(f["id"], {})
            name_votes[f["id"]][f["name"]] = name_votes[f["id"]].get(f["name"], 0) + 1
        canonical = {fid: max(v.items(), key=lambda kv: kv[1])[0] for fid, v in name_votes.items()}

        flows: dict[str, dict] = {}
        for r in runs:
            r["kept"] = r["prompt_id"] in kept
            r.pop("class_names", None)
            r.pop("extra_texts", None)
            f = r["flow"]
            f["name"] = canonical.get(f["id"], f["name"])
            entry = flows.setdefault(f["id"], {"id": f["id"], "name": f["name"], "count": 0})
            entry["count"] += 1
        # 同名工作流补编号，避免筛选器里出现两个一样的名字
        seen_names: dict[str, int] = {}
        for entry in flows.values():
            base = entry["name"]
            seen_names[base] = seen_names.get(base, 0) + 1
            if seen_names[base] > 1:
                entry["name"] = f"{base} #{seen_names[base]}"
        self._json({"ok": True, "comfy": True, "runs": runs,
                    "flows": sorted(flows.values(), key=lambda x: -x["count"]),
                    "pending": sum(1 for r in runs if not r["kept"] and r["status"] == "success")})

    def api_note_asset(self):
        """粘贴图片时前端直接 POST 原始字节，这里存盘并回尺寸。"""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/"):
            return self._err("只接受图片")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._err("空文件")
        if n > NOTE_IMG_MAX:
            return self._err(f"图片太大（上限 {NOTE_IMG_MAX // 1024 // 1024}MB）")
        data = self.rfile.read(n)
        if not data:
            return self._err("空文件")
        return self._json(save_note_asset(data, ctype))

    def serve_note_asset(self, path: str):
        name = path.split("/na/", 1)[-1]
        target = note_asset_path(name)
        if not target:
            return self._err("图片不存在", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def api_notes(self, qs):
        q = (qs.get("q") or [""])[0]
        self._json({"ok": True, "notes": list_notes(q), "dir": str(NOTES_DIR)})

    def api_note(self, raw_id, qs):
        try:
            nid = int(raw_id)
        except ValueError:
            return self._err("总结 id 不对", 400)
        note = load_note(nid)
        if not note:
            return self._err("这篇总结不存在", 404)
        if (qs.get("format") or [""])[0] == "md":
            md = note_as_markdown(note, item_map())
            name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", note.get("title") or f"总结{nid}")[:60]
            body = md.encode("utf-8")
            return self._send(200, body, "text/markdown; charset=utf-8",
                              {"Content-Disposition":
                               "attachment; filename=\"note-%d.md\"; filename*=UTF-8''%s"
                               % (nid, urllib.parse.quote(f"{name}.md"))})
        # 顺带把引用到的案例摘要带出去，前端渲染卡片就不用再逐条请求
        cases = {}
        if note["refs"]:
            for iid, it in item_map().items():
                if iid in note["refs"]:
                    cases[str(iid)] = {
                        "id": iid, "title": it["title"], "flow": it["flow"],
                        "media_url": it["media_url"], "cover_url": it["cover_url"],
                        "media_kind": it["media_kind"], "width": it["width"],
                        "height": it["height"], "duration_s": it["duration_s"],
                        "seed": it["seed"], "tags": it["tags"],
                    }
        self._json({"ok": True, "note": note, "cases": cases})

    def api_vault(self, qs):
        q = (qs.get("q") or [""])[0]
        tag = (qs.get("tag") or [""])[0]
        flow = (qs.get("flow") or [""])[0]
        items = list_items(q, tag, flow)
        tags, flows = {}, {}
        for it in list_items():
            for t in it["tags"]:
                tags[t] = tags.get(t, 0) + 1
            if it["flow_id"]:
                f = flows.setdefault(it["flow_id"], {"id": it["flow_id"], "name": it["flow"], "count": 0})
                f["count"] += 1
        self._json({"ok": True, "items": items, "tags": tags,
                    "flows": sorted(flows.values(), key=lambda x: -x["count"])})

    def api_item(self, item_id):
        with db() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return self._err("记录不存在", 404)
        item = item_to_dict(row)
        item["has_workflow"] = (VAULT_DIR / row["dir_name"] / "workflow_api.json").exists()
        self._json({"ok": True, "item": item})

    def sync_meta(self, row, changes: dict):
        path = VAULT_DIR / row["dir_name"] / "meta.json"
        if not path.exists():
            return
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if changes.get("title") is not None:
            meta["title"] = changes["title"]
        if changes.get("note") is not None:
            meta["note"] = changes["note"]
        if changes.get("tags") is not None:
            tags = changes["tags"]
            meta["tags"] = ([t.strip() for t in tags if str(t).strip()]
                            if isinstance(tags, list) else
                            [t.strip() for t in re.split(r"[,，\s]+", str(tags)) if t.strip()])
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def proxy_view(self, qs):
        """把 ComfyUI 的 /view 转发出来，并透传 Range——视频预览要靠它才能跳转解码。"""
        filename = (qs.get("filename") or [""])[0]
        sub = (qs.get("subfolder") or [""])[0]
        typ = (qs.get("type") or ["output"])[0]
        if not filename:
            return self._err("缺少 filename")
        q = "/view?" + urllib.parse.urlencode(
            {"filename": filename, "subfolder": sub, "type": typ})
        rng = self.headers.get("Range")
        req = urllib.request.Request(comfy_url(q), headers={"Range": rng} if rng else {})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type") or "application/octet-stream"
                upstream_status = r.status
                content_range = r.headers.get("Content-Range")
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                return self._err("Range 无效", 416)
            return self._err(f"ComfyUI 取文件失败: {exc}", 502)
        except Exception as exc:
            return self._err(f"ComfyUI 取文件失败: {exc}", 502)

        extra = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=120"}
        if upstream_status == 206:
            if content_range:
                extra["Content-Range"] = content_range
            return self._send(206, body, ctype, extra)

        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m:
            size = len(body)
            start = int(m.group(1) or 0)
            end = min(int(m.group(2) or size - 1), size - 1)
            if start > end:
                return self._err("Range 无效", 416)
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            return self._send(206, body[start:end + 1], ctype, extra)
        self._send(200, body, ctype, extra)

    def serve_vault_file(self, path: str):
        # 形如 /f/<item_id>/<filename>
        parts = path.split("/", 3)
        if len(parts) < 4 or parts[1] != "f":
            return self._err("路径错误", 400)
        try:
            item_id = int(parts[2])
        except ValueError:
            return self._err("路径错误", 400)
        name = urllib.parse.unquote(parts[3])
        with db() as conn:
            row = conn.execute("SELECT dir_name FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return self._err("记录不存在", 404)
        base = (VAULT_DIR / row["dir_name"]).resolve()
        target = (base / name).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            return self._err("文件不存在", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        size = target.stat().st_size
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            start = int(m.group(1) or 0)
            end = min(int(m.group(2) or size - 1), size - 1)
            if start > end:
                return self._err("Range 无效", 416)
            with target.open("rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._send(200, target.read_bytes(), ctype, {"Accept-Ranges": "bytes"})

    def serve_static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "text/plain"
        if target.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def reindex() -> dict:
    """按 vault 里的 meta.json 重建索引（DB 丢了也能恢复；目录删了也同步清掉）。"""
    added = removed = 0
    with db() as conn:
        # 先清孤儿记录：目录已被删掉，索引里不该还留着
        alive = {p.parent.name for p in VAULT_DIR.glob("*/meta.json")}
        for row in conn.execute("SELECT id, dir_name FROM items").fetchall():
            if row["dir_name"] not in alive:
                conn.execute("DELETE FROM items WHERE id=?", (row["id"],))
                removed += 1
        for meta_path in sorted(VAULT_DIR.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = meta.get("prompt_id")
            if not pid or conn.execute("SELECT id FROM items WHERE prompt_id=?", (pid,)).fetchone():
                continue
            flow = meta.get("flow") or {}
            conn.execute(
                """INSERT OR IGNORE INTO items (prompt_id, dir_name, title, prompt_text,
                    negative_text, tags, note, rating, created_at, kept_at, elapsed_ms, width,
                    height, duration_s, seed, steps, model, flow_id, flow, media_file,
                    cover_file, meta_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, meta_path.parent.name, meta.get("title", ""), meta.get("prompt_text", ""),
                 meta.get("negative_text", ""), ",".join(meta.get("tags", [])),
                 meta.get("note", ""), 0, meta.get("created_at", ""), meta.get("kept_at", ""),
                 meta.get("elapsed_ms", 0), meta.get("width", 0), meta.get("height", 0),
                 meta.get("duration_s", 0), str(meta.get("seed") if meta.get("seed") is not None else ""),
                 meta.get("steps", 0),
                 ((meta.get("models") or {}).get("unet") or (meta.get("models") or {}).get("ckpt") or [""])[0],
                 flow.get("id", ""), flow.get("name", ""), meta.get("media_file", ""),
                 meta.get("cover_file", ""), json.dumps(meta, ensure_ascii=False)),
            )
            added += 1
    return {"added": added, "removed": removed}


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def backfill_flow_names() -> int:
    """给早期收录的记录补上工作流全称（只补缺，不动其它字段）。"""
    idx = workflow_index(force=True)
    if not idx["by_id"]:
        return 0
    fixed = 0
    with db() as conn:
        rows = conn.execute("SELECT id, dir_name, meta_json FROM items").fetchall()
        for row in rows:
            meta_path = VAULT_DIR / row["dir_name"] / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            flow = meta.get("flow") or {}
            if flow.get("workflow_file"):
                continue
            wid = flow.get("workflow_id") or flow.get("id") or ""
            info = idx["by_id"].get(str(wid).lower()) or idx["by_short"].get(str(wid)[:12])
            if not info:
                continue
            flow["name"] = info["name"]
            flow["workflow_file"] = info["file"]
            flow.setdefault("workflow_id", str(wid))
            meta["flow"] = flow
            text = json.dumps(meta, ensure_ascii=False, indent=2)
            meta_path.write_text(text, encoding="utf-8")
            conn.execute("UPDATE items SET flow=?, meta_json=? WHERE id=?",
                         (info["name"], text, row["id"]))
            fixed += 1
    return fixed


def main():
    port = int(os.environ.get("VAULT_PORT") or CFG["port"])
    if not port_free(port):
        print(f"[!] 端口 {port} 已被占用——可能已经在运行，直接打开 http://127.0.0.1:{port}")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 58)
    print("  ComfyUI 经验库已启动")
    print(f"  界面    http://127.0.0.1:{port}")
    print(f"  ComfyUI {CFG['comfy_url']}")
    print(f"  库存    {VAULT_DIR}")
    idx = workflow_index(force=True)
    if idx["by_id"]:
        print(f"  工作流  {idx['dir']}（{len(idx['by_id'])} 个可识别全称）")
    else:
        print("  工作流  未找到 ComfyUI 工作流目录，记录里将只写模型推断名")
    n = backfill_flow_names()
    if n:
        print(f"  补全    历史记录补上工作流全称 {n} 条")
    print("  关闭这个窗口即停止")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
