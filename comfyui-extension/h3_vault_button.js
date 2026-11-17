// comfy-vault 收录按钮 —— 在 ComfyUI 界面左下角加一个「★ 保留这轮」
//
// 设计要点：
//   1. 只在用户点击时收录，不做任何自动记录；
//   2. 全走后端 /api/keep-last，前端不碰文件，避免两套逻辑；
//   3. 幂等：同一轮已收录过，后端会返回错误，这里如实提示；
//   4. 服务没启动时不报错崩掉，只提示怎么启动。
//
// 安装位置（放在「已注册的扩展目录」里，刷新页面即可生效，无需重启 ComfyUI）：
//   <ComfyUI>/custom_nodes/<任意已注册节点>/web/h3_vault_button.js
//   推荐的落点见 README「装进 ComfyUI 界面」一节。
// 依赖本地经验库服务已启动（start.bat / start.sh）。

import { app } from "../../scripts/app.js";

// 经验库服务地址。改了 config.json 里的 port 之后，这里也要跟着改。
const VAULT_BASE = "http://127.0.0.1:8791";
const BTN_ID = "h3vault-keep";
const STYLE_ID = "h3vault-style";

// 便于排查的全局标记（也可在控制台里查：H3Vault.mounted()）
window.H3Vault = {
  version: "1.0",
  mounted: () => !!document.getElementById(BTN_ID),
};

const CSS = `
#${BTN_ID}{
  position:fixed; left:56px; bottom:16px; z-index:2147483000;
  display:inline-flex; align-items:center; gap:7px;
  padding:9px 14px; border-radius:10px; border:1px solid rgba(255,255,255,.16);
  background:linear-gradient(180deg,#5b54f0,#463cd6); color:#fff; cursor:pointer;
  font:600 13px/1.1 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  box-shadow:0 10px 24px -10px rgba(70,60,214,.85);
  transition:transform .12s ease, filter .12s ease, background .18s ease;
  user-select:none; -webkit-user-select:none;
}
#${BTN_ID}:hover{filter:brightness(1.09); transform:translateY(-1px)}
#${BTN_ID}:active{transform:translateY(0) scale(.99)}
#${BTN_ID}[data-state="busy"]{cursor:progress; filter:saturate(.55)}
#${BTN_ID}[data-state="ok"]{background:linear-gradient(180deg,#16a34a,#127c39)}
#${BTN_ID}[data-state="err"]{background:linear-gradient(180deg,#dc2626,#b3261e)}
#${BTN_ID} .hv-star{font-size:13px; line-height:1}
#${BTN_ID} .hv-n{font:500 11px/1 inherit; opacity:.72}

.hv-toast{
  position:fixed; left:56px; bottom:62px; z-index:2147483001; max-width:340px;
  display:flex; gap:10px; align-items:flex-start;
  padding:10px 13px; border-radius:10px;
  background:#151a24; color:#eef1f7; border:1px solid rgba(255,255,255,.14);
  font:500 12.5px/1.55 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  box-shadow:0 18px 40px -16px rgba(0,0,0,.7);
  opacity:0; transform:translateY(6px); transition:opacity .2s, transform .2s;
}
.hv-toast.in{opacity:1; transform:none}
.hv-toast.ok{border-left:3px solid #16a34a}
.hv-toast.err{border-left:3px solid #dc2626}
.hv-toast a{color:#a8b4ff; text-decoration:none; font-weight:600; white-space:nowrap}
.hv-toast a:hover{text-decoration:underline}
.hv-toast .hv-msg{flex:1; min-width:0; word-break:break-word}
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}

let toastTimer = 0;
function toast(msg, kind, link) {
  document.querySelectorAll(".hv-toast").forEach((n) => n.remove());
  const box = document.createElement("div");
  box.className = "hv-toast " + (kind || "");
  const text = document.createElement("div");
  text.className = "hv-msg";
  text.textContent = msg;
  box.appendChild(text);
  if (link) {
    const a = document.createElement("a");
    a.href = link.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = link.label;
    box.appendChild(a);
  }
  document.body.appendChild(box);
  requestAnimationFrame(() => box.classList.add("in"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.classList.remove("in");
    setTimeout(() => box.remove(), 240);
  }, kind === "err" ? 4200 : 3200);
}

async function countItems() {
  try {
    const r = await fetch(VAULT_BASE + "/api/status", { cache: "no-store" });
    if (!r.ok) return null;
    const j = await r.json();
    return typeof j.count === "number" ? j.count : null;
  } catch (e) {
    return null;
  }
}

function mount() {
  // 幂等：万一被加载两次（或 setup 被重复调用），只留一个按钮
  if (document.getElementById(BTN_ID) || !document.body) return false;
  injectStyle();

  const btn = document.createElement("button");
  btn.id = BTN_ID;
  btn.type = "button";
  btn.dataset.state = "idle";
  btn.title = "把最近一次成功的生成连同提示词 / 种子 / 参考图 / 成品一起收录进经验库";
  btn.innerHTML =
    '<span class="hv-star">\u2605</span><span class="hv-label">\u4fdd\u7559\u8fd9\u8f6e</span>' +
    '<span class="hv-n"></span>';

  const label = btn.querySelector(".hv-label");
  const badge = btn.querySelector(".hv-n");
  const idleLabel = "\u4fdd\u7559\u8fd9\u8f6e";

  const refreshBadge = async () => {
    const n = await countItems();
    badge.textContent = n === null ? "" : String(n);
    if (n === null) {
      badge.textContent = "";
      btn.title = "经验库服务未启动：先运行 start.bat / start.sh（地址 " + VAULT_BASE + "）";
    }
  };

  let busy = false;
  btn.onclick = async () => {
    if (busy) return;
    busy = true;
    btn.dataset.state = "busy";
    label.textContent = "\u6536\u5f55\u4e2d\u2026";
    try {
      const res = await fetch(VAULT_BASE + "/api/keep-last", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      let j = {};
      try {
        j = await res.json();
      } catch (e) {
        j = {};
      }
      if (res.ok && j && j.ok) {
        btn.dataset.state = "ok";
        label.textContent = "\u5df2\u6536\u5f55";
        const meta = j.meta || {};
        const title = (meta.title || "").slice(0, 24);
        toast("\u5df2\u6536\u5165\u7ecf\u9a8c\u5e93" + (title ? "\uff1a" + title : ""), "ok", {
          url: VAULT_BASE,
          label: "\u6253\u5f00",
        });
        refreshBadge();
      } else {
        btn.dataset.state = "err";
        label.textContent = "\u6536\u5f55\u5931\u8d25";
        toast((j && j.error) || "\u6536\u5f55\u5931\u8d25", "err");
      }
    } catch (e) {
      btn.dataset.state = "err";
      label.textContent = "\u6536\u5f55\u5931\u8d25";
      toast("\u8fde\u4e0d\u4e0a\u7ecf\u9a8c\u5e93\u670d\u52a1\uff08" + VAULT_BASE + "\uff09\uff1a\u5148\u8fd0\u884c start.bat / start.sh", "err");
    }
    setTimeout(() => {
      btn.dataset.state = "idle";
      label.textContent = idleLabel;
      busy = false;
    }, 2400);
  };

  document.body.appendChild(btn);
  refreshBadge();
  return true;
}

// 触发方式保留两条，谁先到算谁的（mount 自身幂等）：
//   1) registerExtension 的 setup —— 常规路径
//   2) 模块求值后立刻尝试 —— 前端在 app 就绪后才 import 时也不会漏
app.registerExtension({ name: "h3vault.keepButton", setup: mount });

(function boot(tries) {
  if (mount()) return;
  if (tries > 0) setTimeout(() => boot(tries - 1), 150);
})(60);

// 看护：前端完成初始化时会重刷 body 子节点，按钮可能被一并清掉。
// 这里低频巡检，掉了就补回来（getElementById 开销可忽略）。
setInterval(() => {
  if (!document.getElementById(BTN_ID)) mount();
}, 1200);
