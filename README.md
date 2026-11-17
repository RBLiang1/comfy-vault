# comfy-vault

**给 ComfyUI 用的「手动收录」经验库。** 跑完一轮觉得满意，点一下就把这一轮的
提示词 / 种子 / 参考图 / 成品 / 参数 / 耗时整包存进本地库；不满意就直接跳过，什么都不记。

生成完了还能在库里写**经验总结**：既写跨案例的通用结论，也能把任意一条已收录的案例挂进正文当例子。

> A local "keeper" for your ComfyUI runs. Nothing is recorded automatically — you look at the
> result first, then press one button to keep that run (prompt, seed, reference images, output,
> parameters, duration). Then write experience notes that cite real cases by id.
> Pure Python standard library, no dependencies, no cloud, no telemetry.

![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)
![For](https://img.shields.io/badge/works%20with-ComfyUI-4b5563)

<!-- 截图：把界面截图存成 docs/screenshot.png，然后取消下面这行注释
![screenshot](docs/screenshot.png)
-->

---

## 为什么不做全自动

没经过筛选的记录等于没记录。

ComfyUI 的执行历史里躺着几百轮结果，其中大多数是试错。把它们全存下来的结果是：
库越攒越大，真正能复用的那几条反而找不到了。所以这个工具**刻意**做成「先看，再决定」——
记录动作由你触发，一次一轮。

---

## 它做什么

- **一键收录一轮**：网页上点「保留这轮」，或在 ComfyUI 界面左下角点「★ 保留这轮」。
- **整包复制，不是路径索引**：成品、参考图会被真的下载到库目录里。之后清理
  `ComfyUI/output/`、换盘、删历史，库都不受影响。
- **不限模型、不限工作流**：不写死节点 id，也不写死某个模型的字段名，按通用特征去捞
  （见[下文](#它怎么做到不限模型--不限工作流)）。换新工作流不用改代码。
- **记录工作流全称**：同一个模型下有好几个工作流时，能分清是哪一个。
- **自包含文件夹**：每条记录是一个独立文件夹，里面 `meta.json` + 工作流快照 + 成品 + 参考图，
  直接拷给别人也能看懂，不依赖数据库。
- **经验总结**：写整体经验，正文里用 `[[12]]` 引用第 12 条案例，卡片只存 id、内容永远跟库同步。
- **纯标准库**：`python app.py` 就能跑，不需要 pip install 任何东西。

---

## 快速开始

**前提**：ComfyUI 正在运行（默认 `http://127.0.0.1:8188`），且本机能直连、无需鉴权。

```bash
git clone https://github.com/<your-name>/comfy-vault.git
cd comfy-vault
```

Windows 双击 `start.bat`；macOS / Linux：

```bash
chmod +x start.sh
./start.sh
```

或者直接 `python app.py`（Python 3.9+）。

然后打开 <http://127.0.0.1:8791>。首次运行会自动生成 `config.json`，窗口保持打开即为服务运行中，
关掉窗口 / `Ctrl+C` 就是停止。

> 端口 8791 被占用时改 `config.json` 里的 `port`。如果装了 ComfyUI 内的按钮，
> 还要同步改 `comfyui-extension/h3_vault_button.js` 顶部的 `VAULT_BASE`。

---

## 装进 ComfyUI 界面（可选）

网页版够用，但跑完图时人在 ComfyUI 里，不想切窗口。加一个按钮：

**方式 A —— 免重启（推荐）**

把 `comfyui-extension/h3_vault_button.js` 复制到任意一个**已注册节点**的 `web/` 目录下，例如：

```
<ComfyUI>/custom_nodes/ComfyUI-RefGallery/web/h3_vault_button.js
```

ComfyUI 每次加载页面都会重扫这些目录，**刷新浏览器（F5）就生效，不用重启 ComfyUI**。

**方式 B —— 标准扩展**

```
<ComfyUI>/custom_nodes/comfy-vault-button/web/h3_vault_button.js
```

这种方式需要重启一次 ComfyUI。

装好后界面**左下角**会出现紫色按钮 `★ 保留这轮 N`（N = 当前库里有多少条）。

- 点它 = 收录**最近一次成功**的执行，等价于网页上的「保留这轮」；
- 成功后弹出「已收录：标题」，带一个「打开」跳到经验库；
- 同一轮点第二次会提示「这一轮已经收录过了」，不会重复入库；
- 服务没启动时按钮不会崩，只提示你先运行 `start.bat` / `start.sh`。

> 按钮位置固定在左下角是故意的：右下角常被显存/硬件监控类插件占着。

---

## 收录了什么

| 记的东西 | 从哪来 |
|---|---|
| 提示词 / 反向提示词 | 执行历史里的输入值（含上游节点算出来的文本） |
| 种子 | 采样器 / 噪声节点 |
| 步数 / 采样器 / CFG / denoise / shift | 采样相关输入，连线到 Primitive 节点的会顺藤取值 |
| 成品（图片 / 视频 / 音频） | 节点 `outputs`，可多个 |
| 分辨率 / 时长 / 帧率 | 输出自带的 metadata，缺了读文件头或节点参数 |
| 耗时 | `execution_start` → `execution_success` 的时间差 |
| 参考图 / 首帧 / 尾帧 / 遮罩 | 输入里的媒体文件名，连同文件一起复制进库 |
| 工作流 id + **全称** | `workflow_metadata.workflow_id` + 本地已保存工作流文件反查 |
| 模型 / LoRA | 节点输入里的 `.safetensors` / `.ckpt` / `.pt` |

---

## 它怎么做到「不限模型 / 不限工作流」

不写死任何节点 id、也不写死某个模型的字段名，全部按通用特征去捞：

| 要记的东西 | 识别方式 |
|---|---|
| 提示词 | 字段名是 `prompt` / `text` / `text_g` … 的输入，以及**上游节点输出的文本**（如 `fisher_prompt` 这类由前处理节点算出来的提示词）；纯数字、比例之类的假文本会被过滤 |
| 反向提示词 | 字段名含 `negative` / `neg_` 的文本 |
| 种子 | `seed` / `noise_seed` / `rand_seed`，优先取采样器、噪声节点的；字符串形式的数字也认 |
| 步数 / 采样器 / CFG / denoise | `steps` / `sampler_name` / `scheduler` / `cfg` / `denoise` / `shift_*`；参数被「连线」到 Primitive 节点时会顺着连线取值 |
| 参考图 / 首帧 / 遮罩 | `first_frame` / `last_frame` / `image` / `init_image` / `ref_image_N` … 且值带媒体后缀 |
| 成品 | `outputs` 里所有图片 / 视频 / 音频，排除 128×128 以下的占位预览；图片尺寸缺失时直接读文件头（PNG/WebP/JPEG） |
| 分辨率 / 时长 / 帧率 | 优先用输出自带的 metadata；没有就退回节点里的 `duration_seconds` 等参数 |
| 耗时 | `execution_start` → `execution_success` 的时间差 |
| 工作流归属 | `workflow_metadata.workflow_id`（或 `extra_pnginfo.workflow.id`），同一工作流自动归到一组 |
| **工作流全称** | 用上面的 id 去 ComfyUI 的「已保存工作流」目录里查同名文件，把**文件全名**写进记录（见下文） |

实测覆盖过文生视频、图生视频、放大链、姿态参考、纯 KSampler 图片生成这几类工作流的差异，
提示词、种子、参考图、成品、耗时都能正确取到。**换新工作流不用改代码**，实在取不到可以在
`app.py` 顶部的 `TEXT_KEYS` / `NEG_KEYS` 里补字段名。

### 工作流全称（同模型多工作流也能分清）

ComfyUI 的执行历史里只有一个 `workflow_id`（UUID），没有可读名字。程序会拿这个 id 去
`<ComfyUI>/user/default/workflows/**.json` 里反查——这些文件里存着同一个 id，
**文件名就是你在 ComfyUI 里看到的工作流全称**，于是每条记录都会写上：

```json
"flow": {
  "id": "d42d6f0e-7e7",
  "name": "MiniMax - H3 智能一体化视频创作",
  "workflow_file": "MiniMax - H3 智能一体化视频创作.json",
  "workflow_id": "d42d6f0e-7e73-4e86-bc76-044ec9024404",
  "family": "MiniMax H3",
  "derived_name": "MiniMax H3 · 放大"
}
```

同一个模型下的多个工作流（例如 `…智能一体化 V3` 和 `…+ 总控版`）会各归各的，
按全称搜索、按全称筛选都能用；详情页还会显示它对应的 `.json` 文件名。

- 目录默认自动探测（Windows 下从运行中的 ComfyUI 进程命令行里读 `main.py` 位置），
  也可以用 `COMFYUI_DIR` 环境变量指定，探测不到时在 `config.json` 里填 `comfy_workflows_dir`。
- 同 id 的 `.bak` / `_before_*` 副本会被自动忽略，取正式那一份的名字。
- 老记录启动时会自动补齐全称（只补字段，不改其它内容）。

---

## 界面

**待筛选**：ComfyUI 最近的执行历史（默认 60 条），按时间倒序，可按工作流、提示词、种子筛选。

- 卡片左上角标签会显示**工作流全称**（太长会截断，鼠标悬停看完整名或文件名）
- `保留这轮` —— 一键收录，标题自动取提示词里的一句话
- `加备注` —— 先填标题 / 标签 / 备注再收
- 已收录的会标 `已收录`，按钮变成「已在经验库 · 查看」

**经验库**：已收录的全部记录。支持搜索（标题 / 提示词 / 备注 / 种子 / 标签 / **工作流全称**）、
按工作流和标签筛选。点开详情能看到：工作流全称与对应 `.json` 文件名、成品播放、参数表、
模型与 LoRA、参考图、完整提示词和反向提示词，可改标题 / 标签 / 备注，可一键复制全套参数
（含提示词，方便下次复用），`移出经验库` 会把文件夹移到 `vault/_trash`（不会立刻真删）。
详情底部还有 **被总结引用**：哪几篇经验总结引用了这一条，点一下直接跳过去。

---

## 经验总结

第三个标签页。左边是总结列表，右边是编辑器 + 实时预览，自动保存（停手 1 秒即存，`Ctrl+S` 立即存）。

- 正文是轻量 Markdown：`# 标题`、`- 列表`、`1. 有序`、`> 引用`、`**加粗**`、单行 `代码`，
  以及用三个反引号包起来的代码块
- 想举某个具体例子：点 **插入案例**，弹出经验库列表（可搜索），点一下就在光标处插入一张卡片；
  可连续插入多个，插完点「完成」
- 也可以手写 `[[12]]`（12 = 案例 id），或写 `[[12|这段是用来演示逆光的]]` 给它加个说明
- 案例卡**不复制内容**，只存 id：标题、工作流、参数永远跟着经验库里的最新状态走，
  点开就是那一轮的完整详情
- 反过来也行：在案例详情里点 **+ 加入一篇总结**，把这一条追加到已有总结，或新建一篇放进去
  （同一篇里已有该案例会提示，不重复插）
- **导出 .md** 会把案例引用展开成「案例 #12 · 标题 + 工作流 + 参数 + 提示词」，方便贴到别处
- 删除只是移到 `notes/_trash`

写法和用途：一篇总结可以同时是「通用结论」和「案例索引」，比如

```
## 打光
侧逆光 + 低补光最稳，8 步就够，再多只会让皮肤发灰。

[[19]]

## 什么时候会崩
时长超过 8s、或者首帧动作太大时，手部开始糊。看这个反例：
[[17]]
```

---

## 数据存在哪

全部在程序目录下面（`vault_dir` / `notes_dir` 可改成绝对路径换盘）：

```
vault/
  20261117-200559_某轮标题/
    meta.json          # 全部字段（工作流全称、提示词、种子、参数、耗时、来源 prompt_id…）
    workflow_api.json  # 那一轮的 API 格式工作流快照，可回看/复现
    <成品文件>          # 视频或图片
    ref1_xxx.png       # 参考图 / 首帧，按顺序编号
    ref2_xxx.png
  _trash/              # 移出经验库的条目挪到这里
data/vault.sqlite3     # 检索索引（可重建）
notes/
  1.json               # 一篇经验总结：标题、正文（含 [[案例id]]）、时间
  _trash/
```

**图片和视频是真复制出来的，不是路径索引。** 点「保留」时，程序通过 ComfyUI 的 `/view`
接口把文件字节读下来，写进上面那个文件夹（`target.write_bytes(...)`）。所以：

- 之后清理 `ComfyUI/output/`、换盘、删 ComfyUI 历史，经验库都不受影响；
- 代价是占一份额外空间——同一份视频在 ComfyUI 输出目录和 `vault/` 里各存一份；
- 参考图同样会被复制（`ref1_`、`ref2_` 前缀），因为 ComfyUI 的 `input/` 里同名文件随时会被覆盖。

其它几点：

- 每个条目是**自包含文件夹**，直接拷走也能看懂，不依赖数据库。
- 数据库丢了不要紧：`POST /api/reindex` 会扫描所有 `meta.json` 重建索引。
- 想换存储盘：改 `config.json` 的 `vault_dir` 为绝对路径（比如 `D:\\ComfyVault`）。

---

## 配置

`config.json`（首次运行自动生成，字段含义见 `config.example.json`）：

```json
{
  "comfy_url": "http://127.0.0.1:8188",
  "port": 8791,
  "vault_dir": "vault",
  "notes_dir": "notes",
  "pending_limit": 60,
  "comfy_workflows_dir": ""
}
```

- `comfy_url`：ComfyUI 跑在别的端口/机器上时改这里（需本机可直连、无需鉴权）。
- `comfy_workflows_dir`：ComfyUI 已保存工作流目录，留空自动探测；
  探测不对时手动填 `<ComfyUI>/user/default/workflows` 的绝对路径。
- `vault_dir`：`vault/` 放哪，可写绝对路径换盘。
- `notes_dir`：经验总结存哪，同样可写绝对路径。
- `pending_limit`：待筛选列表一次拉多少条历史。

---

## 自测

```bash
python tests/smoke_test.py
```

跑的是**真实** ComfyUI 历史：解析、收录、落盘、参考图复制、搜索、改备注、Range 播放、
路径穿越防护、经验总结的新建/引用/导出/搜索/删除、索引重建，共 44 项检查。

会临时创建一条「自测-图片收录」和一篇「自测-总结经验」（跑完自动删总结，条目留在库里可自行移出）。
所以需要 ComfyUI 正在运行、历史里至少有一条带图片的记录。

---

## 目录结构

```
app.py                       # 全部后端：HTTP 服务 + 解析 + 收录 + 总结（约 1.7k 行，零依赖）
web/index.html               # 单文件前端（三个标签页，无构建步骤）
comfyui-extension/           # ComfyUI 界面内的「保留这轮」按钮
tests/smoke_test.py          # 对着真实 ComfyUI 跑的自测
config.example.json          # 配置示例
start.bat / start.sh         # 启动脚本
```

---

## 常见问题

- **端口被占用**：`config.json` 改 `port`，或看窗口提示「已经在运行」直接打开旧地址。
- **顶部显示「ComfyUI 未连接」**：ComfyUI 没开，或端口不是 8188，改 `comfy_url`。
- **视频卡片是深色占位**：鼠标移上去才拉流预览（避免一屏几十个视频同时下载）。
  点开详情是完整播放器。
- **时间对不上**：耗时是「开始执行→执行成功」，不含你在队列里排队的时间。
- **找不到某轮记录**：ComfyUI 历史被清理了。收录要在历史还在的时候点。
- **工作流只显示 UUID 前缀**：没探测到已保存工作流的目录，填 `comfy_workflows_dir`。
- **ComfyUI 里的按钮点了没反应**：先确认经验库服务在跑，再看浏览器控制台里
  `H3Vault.mounted()` 是否为 `true`。

---

## 已知限制

- 只对「ComfyUI 历史还在」的轮次有效；历史被清理后无法补录。
- 需要能直连 ComfyUI，暂不支持带鉴权或走反代的部署。
- 工作流全称依赖本地 `user/default/workflows/` 里有对应文件，纯 API 提交的工作流只能拿到 UUID。
- 单机使用；没有多用户、没有同步。
- 界面目前只有中文。

---

## License

[MIT](LICENSE)
