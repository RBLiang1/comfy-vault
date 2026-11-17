#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：用真实 ComfyUI 历史验证解析 + 收录 + 回读。可反复跑。

用法：  python tests/smoke_test.py
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "vault"
DB = ROOT / "data" / "vault.sqlite3"

# 端口从 config.json 读，改过 port 也不用动这里
_port = 8791
try:
    _port = int(json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("port") or 8791)
except Exception:
    pass
BASE = f"http://127.0.0.1:{_port}"

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        fails.append(name)


def req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def api(path, method="GET", body=None):
    code, raw, _ = req(path, method, body)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:
        return code, {"raw": raw[:200]}


print("1) 状态 / 待筛选")
code, st = api("/api/status")
check("status 200", code == 200, st)
check("ComfyUI 已连接", st.get("comfy") is True, st.get("comfy_error"))
code, pend = api("/api/pending?limit=60")
runs = pend.get("runs") or []
check("pending 有记录", code == 200 and len(runs) > 0, code)
check("pending 返回工作流分组", len(pend.get("flows") or []) >= 2, pend.get("flows"))

# 找出两类（视频 / 图片）成功记录
video = next((r for r in runs if r["status"] == "success" and r["media_kind"] == "video"), None)
image = next((r for r in runs if r["status"] == "success" and r["media_kind"] == "image"), None)
check("找到视频类成功记录", video is not None)
check("找到图片类成功记录", image is not None)

if video:
    print(f"   视频样本 flow={video['flow']['name']} seed={video['seed']} "
          f"{video['width']}x{video['height']} {video['duration_s']}s")
    check("视频：有提示词", len(video["prompt_text"]) > 20, len(video["prompt_text"]))
    check("视频：有种子", video["seed"] is not None, video["seed"])
    check("视频：有分辨率", video["width"] > 0 and video["height"] > 0)
    check("视频：有时长", video["duration_s"] > 0, video["duration_s"])
    check("视频：有耗时", video["elapsed_ms"] > 0, video["elapsed_ms"])
    check("视频：有参考图", len(video["refs"]) > 0)

if image:
    print(f"   图片样本 flow={image['flow']['name']} seed={image['seed']} "
          f"{image['width']}x{image['height']} {image['elapsed_ms']}ms")
    check("图片：有提示词", len(image["prompt_text"]) > 3, image["prompt_text"][:40])
    check("图片：有种子", image["seed"] is not None, image["seed"])
    check("图片：有分辨率", image["width"] > 0 and image["height"] > 0)
    check("图片：有耗时", image["elapsed_ms"] > 0)
    check("图片：提示词不是数字垃圾",
          not all(ch.isdigit() or ch in ",.x " for ch in image["prompt_text"]))

print("2) 收录真实记录（挑一轮还没收录过的）")
kept_ids = []
target = next((r for r in runs
               if r["status"] == "success" and r.get("media_url") and not r.get("kept")), None)
if target is None:
    print("   历史里的成功记录都已经收录过了，跳过收录步骤（下面用库里已有记录做只读测试）")
else:
    print(f"   收录样本 flow={target['flow']['name']} kind={target['media_kind']}")
    code, res = api("/api/keep", "POST", {"prompt_id": target["prompt_id"],
                                          "title": "自测-图片收录", "tags": "自测,图片",
                                          "note": "smoke test"})
    if not res.get("ok") and "已经收录" in str(res.get("error")):
        # 极端情况：刚好在我选完之后被收录了，换一条重试
        alt = next((r for r in runs if r["status"] == "success"
                    and r.get("media_url") and not r.get("kept")), None)
        if alt:
            target = alt
            code, res = api("/api/keep", "POST", {"prompt_id": target["prompt_id"],
                                                  "title": "自测-图片收录", "tags": "自测,图片",
                                                  "note": "smoke test"})
    check("收录成功", res.get("ok") is True, res)
    if res.get("ok"):
        kept_ids.append(res["id"])
        d = Path(res["dir"])
        check("落盘目录存在", d.is_dir(), d)
        check("meta.json 存在", (d / "meta.json").is_file())
        check("workflow_api.json 存在", (d / "workflow_api.json").is_file())
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        check("meta 含提示词", len(meta.get("prompt_text", "")) > 3)
        check("meta 含种子", meta.get("seed") is not None, meta.get("seed"))
        check("meta 含工作流名", bool(meta.get("flow", {}).get("name")), meta.get("flow"))
        check("meta 含分辨率", meta["width"] > 0 and meta["height"] > 0)
        media = [m for m in meta.get("media", []) if m.get("stored")]
        check("成品文件已复制", len(media) > 0)
        for m in media:
            f = d / m["stored"]
            check(f"成品文件非空 {m['stored']}", f.is_file() and f.stat().st_size > 1000,
                  f.stat().st_size if f.is_file() else "missing")
        refs = [r for r in meta.get("refs", []) if r.get("stored")]
        if target["refs"]:
            check("参考图已复制", len(refs) == len(target["refs"]), f"{len(refs)}/{len(target['refs'])}")

print("3) 回读 / 搜索 / 改备注 / 文件服务")
code, vault = api("/api/vault")
vitems = vault.get("items", [])
check("经验库可读", code == 200 and len(vitems) > 0)
if vitems:
    probe = vitems[0]
    kw = (probe["title"] or probe["prompt_text"] or "")[:8].strip()
    if kw:
        code, vault2 = api("/api/vault?q=" + urllib.parse.quote(kw))
        check("按关键词搜索命中",
              any(x["id"] == probe["id"] for x in vault2.get("items", [])),
              len(vault2.get("items", [])))
if kept_ids:
    iid = kept_ids[0]
    code, one = api(f"/api/item/{iid}")
    check("详情接口", code == 200 and one["item"]["id"] == iid)
    item = one["item"]
    code, patched = api(f"/api/item/{iid}", "PATCH", {"note": "改过的备注", "tags": "自测,已改"})
    check("PATCH 生效", patched["item"]["note"] == "改过的备注", patched["item"]["note"])
    meta_path = VAULT / item["dir_name"] / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    check("PATCH 同步写回 meta.json", meta["note"] == "改过的备注", meta.get("note"))
    # 文件服务 + Range
    code, raw, hdrs = req(item["media_url"])
    check("媒体文件可下载", code == 200 and len(raw) > 1000, f"{code} {len(raw)}")
    code, part, h2 = req(item["media_url"])  # 简单二次请求，确认稳定
    r2 = urllib.request.Request(BASE + item["media_url"], headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(r2, timeout=30) as resp:
        seg = resp.read()
        check("Range 请求返回 206/部分内容",
              resp.status == 206 and len(seg) == 1024, f"{resp.status} {len(seg)}")
    # 路径穿越防护
    code, _, _ = req(f"/f/{iid}/../../app.py")
    check("拒绝路径穿越", code in (400, 404), code)

print("4) 数据库一致性")
with sqlite3.connect(DB) as conn:
    n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    check("DB 有记录", n >= 1, n)
    row = conn.execute("SELECT prompt_id, flow, seed FROM items ORDER BY id DESC LIMIT 1").fetchone()
    check("DB 存了 flow/seed", bool(row[1]), row)

print("5) 经验总结（写总结 + 挂案例）")
code, made = api("/api/notes", "POST", {"title": "自测-总结经验"})
check("新建总结", code == 200 and bool(made.get("note", {}).get("id")), made)
nid = (made.get("note") or {}).get("id")
case_id = kept_ids[0] if kept_ids else (vitems[0]["id"] if vitems else None)
if nid and case_id:
    ref_id = case_id
    content = (f"# 自测\n这一轮的做法：\n\n[[{ref_id}]]\n\n"
               f"手写引用也行 [[{ref_id}|加个说明]]")
    code, saved = api(f"/api/note/{nid}", "PUT",
                      {"title": "自测-总结经验", "content": content})
    check("保存正文", code == 200, saved)
    check("解析出案例引用", saved["note"]["refs"] == [ref_id], saved["note"]["refs"])
    code, full = api(f"/api/note/{nid}")
    check("回读带案例数据", str(ref_id) in (full.get("cases") or {}),
          list((full.get("cases") or {}).keys()))
    if str(ref_id) in (full.get("cases") or {}):
        check("案例数据含标题/工作流",
              bool(full["cases"][str(ref_id)]["title"]) and bool(full["cases"][str(ref_id)]["flow"]))
    code, lst = api("/api/notes")
    check("总结列表含引用数",
          any(n["id"] == nid and n["refs"] == [ref_id] for n in lst.get("notes", [])),
          lst.get("notes"))
    code, raw_md, hdrs = req(f"/api/note/{nid}?format=md")
    md = raw_md.decode("utf-8")
    check("导出 md 展开案例", code == 200 and "案例 #" in md, md[:100])
    check("md 的 Content-Type", "markdown" in (hdrs.get("Content-Type") or ""),
          hdrs.get("Content-Type"))
    code, s2 = api("/api/notes?q=" + urllib.parse.quote("自测-总结"))
    check("总结可搜索", any(n["id"] == nid for n in s2.get("notes", [])),
          [n["title"] for n in s2.get("notes", [])])
    code, gone = api(f"/api/note/{nid}", "DELETE")
    check("删除总结", gone.get("ok") is True, gone)
    check("删掉的在 notes/_trash", (ROOT / "notes" / "_trash" / f"{nid}.json").is_file())

print("6) 索引重建（模拟 DB 丢失）")
code, re_idx = api("/api/reindex", "POST", {})
check("reindex 可用", code == 200, re_idx)

print()
if fails:
    print(f"❌ {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("✅ 全部通过")
