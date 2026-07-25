# Wiki AI 翻译流水线

通过 GitHub Actions 定时拉取 Wiki 页面，调用 OpenAI 兼容 API 翻译为目标语言，并可选自动推送回任意 wiki。基于 **uv + httpx + openai SDK + python-dotenv + tenacity** 构建。

## 目录结构

```
.
├── .github/workflows/translate.yml   # CI 工作流（uv + setup-uv）
├── src/wiki_translate/                 # 主包
│   ├── cli.py                          # 命令行入口
│   ├── config.py                       # .env 加载 + 强类型 dataclass
│   ├── fandom.py                       # MediaWiki API 客户端 / Publisher（httpx）
│   ├── translator.py                   # OpenAI SDK 封装（带 tenacity 重试）
│   ├── fetch.py                        # 阶段一：拉取源 wiki → cache + manifest
│   ├── pipeline.py                     # 阶段二：process（翻译/复制）+ merge-state + 复用辅助
│   ├── upload.py                       # 阶段三：统一推送页面/文件/系统消息到目标 wiki
│   ├── manifest.py                     # 清单：fetch 产出、process/upload 消费
│   ├── files.py                        # 文件列举/下载/MIME 辅助
│   ├── system.py                       # 系统消息列举/分类辅助
│   ├── diff.py                         # unified diff 生成
│   ├── segmenter.py                    # diff 级局部翻译的段落规划/拼接
│   ├── glossary.py                     # 术语表加载与注入
│   ├── state.py                        # state.json 持久化 + delta 合并
│   └── utils.py                        # 切块、文件名、剥离代码块
├── prompts/system.txt                # 翻译用 system prompt（可定制术语库）
├── pyproject.toml                    # 项目与依赖（uv 管理）
├── uv.lock                           # 依赖锁定（uv sync 生成）
├── .env                              # 流水线配置（可提交，不写入密钥）
├── .env.example                      # 配置模板
├── manifest.json                     # 待处理/待上传清单（fetch 生成）
├── state.json                        # 增量状态（运行时生成）
├── cache/incoming/                   # fetch 抓到的新源码（process 的输入）
├── cache/source/                     # diff 基线（process 推进）
├── cache/files/                      # 下载的文件二进制（upload 用）
└── output/<lang>/*.wiki              # 翻译产物
```

## 技术栈

| 关注点 | 选型 | 原因 |
|---|---|---|
| 包管理 | **uv** | 极快、原生支持 PEP 621、自带锁文件 |
| HTTP | **httpx** | 同步/异步统一 API、HTTP/2、context manager 原生 |
| LLM | **openai SDK ≥ 1.40** | 官方维护、自动适配 OpenAI 兼容端点（DeepSeek、火山方舟等） |
| .env | **python-dotenv** | 业界标准 |
| 重试 | **tenacity** | 声明式退避策略，比手写循环可读 |
| 项目布局 | **src layout + hatchling** | 现代 PEP 标准，避免 import 路径污染 |

## 快速开始

```sh
# 1. 安装 uv（已装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步依赖
uv sync

# 3. 配 API Key
export OPENAI_API_KEY="sk-..."

# 4. 编辑 .env：把 WIKI_API_URL 等改成目标 wiki

# 5. 试跑（不调用 LLM）
uv run wiki-translate --env .env --dry-run

# 6. 真翻译
uv run wiki-translate --env .env
```

CLI 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--env` | `.env` | 环境变量配置文件 |
| `--mode` | `all` | `fetch` / `process` / `upload` / `merge-state` / `all` |
| `--force` | off | 忽略 state 强制重新处理/推送 |
| `--dry-run` | off | 仅枚举与比对，不调用 LLM、不修改 wiki、不写文件 |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

三阶段说明：

- `fetch`：拉取源 wiki 全部内容（主条目/模板/分类/Lua/CSS/JS + 文件 + 系统消息），写入 `cache/` 并生成 `manifest.json`。只读源 wiki。
- `process`：读 `manifest.json`，对页面/系统消息做 LLM 翻译（Lua 模块、CSS/JS 等代码原样复制），产出 `output/`。不触达任何 wiki API，可分片并行。
- `upload`：读 `manifest.json` 与 `output/`，把页面、系统消息、文件统一推送到目标 wiki，集中控制频率规避限流。
- `all`：本地顺序执行 fetch → process → upload。

## 配置项一览（.env）

| 键 | 含义 | 示例 |
|---|---|---|
| `WIKI_API_URL` | 源 wiki 的 api.php | `https://xxx.fandom.com/api.php` |
| `WIKI_SOURCE_LANG` / `WIKI_TARGET_LANG` | 源/目标语言代码 | `en` / `zh` |
| `WIKI_NAMESPACES` | 命名空间，逗号分隔 | `0,10,14` |
| `WIKI_CATEGORY` | 抓取某分类下的页面 | `Characters` |
| `WIKI_PAGE_LIST` | 固定页面列表，逗号分隔 | `Home,Rules` |
| `WIKI_ALL_PAGES` | 是否遍历整个 wiki | `true` |
| `WIKI_FILTER_REDIRECTS` | `nonredirects` / `redirects` / `all` | `nonredirects` |
| `WIKI_MAX_PAGES` | 抓取上限，0=不限 | `0` |
| `WIKI_SLEEP_BETWEEN` | 每页之间间隔（秒） | `0.3` |
| `WIKI_USER_AGENT` | 自定义 UA，建议带联系方式 | `MyBot/1.0 (contact: ...)` |
| `TRANSLATOR_BASE_URL` / `OPENAI_BASE_URL` | LLM 端点 | `https://api.openai.com/v1` |
| `TRANSLATOR_MODEL` / `OPENAI_MODEL` | 模型名 | `gpt-4o-mini` |
| `OPENAI_API_KEY` | 密钥（建议走真实环境变量） | `sk-...` |
| `TRANSLATOR_TEMPERATURE` / `TRANSLATOR_MAX_TOKENS` | 采样参数 | `0.2` / `4096` |
| `TRANSLATOR_CHUNK_CHARS` | 长页面切块阈值 | `3500` |
| `TRANSLATOR_RETRY` / `TRANSLATOR_RETRY_DELAY` | 调用失败重试次数与退避 | `3` / `5` |
| `TRANSLATOR_SYSTEM_PROMPT_FILE` | system prompt 文件路径 | `prompts/system.txt` |
| `TRANSLATOR_SYSTEM_PROMPT` | 直接用字符串覆盖（高优先级） | |
| `OUTPUT_DIR` / `OUTPUT_STATE_FILE` | 输出目录与 state 文件 | `output` / `state.json` |
| `PUBLISH_ENABLED` | 是否启用推送 | `false` |
| `PUBLISH_API_URL` | 目标 wiki 的 api.php | |
| `PUBLISH_SUMMARY` | edit summary | `AI 自动翻译同步` |
| `PUBLISH_BOT_FLAG` / `PUBLISH_MINOR` | 是否标记为 bot/minor | `true` / `false` |
| `PUBLISH_SLEEP_BETWEEN` | 每次 edit 间隔 | `1.0` |
| `PUBLISH_TITLE_PREFIX` / `PUBLISH_TITLE_SUFFIX` | 标题前后缀 | |
| `PUBLISH_TITLE_MAP` | 显式标题映射（JSON） | `{"Home":"首页"}` |
| `FANDOM_BOT_USER` / `FANDOM_BOT_PASSWORD` | BotPassword 凭据 | 走 GitHub Secrets |
| `MAX_PAGES_PER_RUN` | 单次运行最多处理多少页 | `0` |

> 列表用 **逗号** 分隔；字典用 **JSON 字符串**；多行 prompt 放到独立文件并用 `*_FILE` 引用。

## Dry-run 模式

无需 `OPENAI_API_KEY` 也能跑，适合：

- 接入新 wiki 时确认抓取范围
- 估算 token / 费用（输出末尾会打印总字符数）
- 验证 `WIKI_NAMESPACES` / `WIKI_CATEGORY` / `WIKI_FILTER_REDIRECTS` 配置

```sh
uv run wiki-translate --env .env --dry-run
uv run wiki-translate --env .env --mode publish --dry-run   # 推送前预览
```

GitHub Actions 上手动触发时把 `dry_run` 选成 `true` 也进入该模式（commit 步骤会被自动跳过）。

## 推送回目标 Wiki

### 1. 申请 BotPassword

⚠️ **不要使用账号密码**：

1. 用目标账号登录目标 wiki
2. 访问 `https://YOUR-TARGET-WIKI.fandom.com/wiki/Special:BotPasswords`
3. 至少勾选：`Edit existing pages` / `Create, edit, and move pages` / `High-volume editing`
4. 保存生成的 `账号@bot标签` 与密码

### 2. 配置 Secrets

仓库 `Settings → Secrets and variables → Actions`：

- `OPENAI_API_KEY`
- `FANDOM_BOT_USER` = `MyAccount@MyBot`
- `FANDOM_BOT_PASSWORD` = BotPassword 密码
- 可选 `OPENAI_BASE_URL` 覆盖 `.env` 端点

### 3. 在 `.env` 中开启

```ini
PUBLISH_ENABLED=true
PUBLISH_API_URL=https://target-wiki.fandom.com/zh/api.php
PUBLISH_BOT_FLAG=true
PUBLISH_TITLE_MAP={"Home":"首页","Rules":"规则"}
```

### 4. 触发推送

GitHub Actions 手动触发把 `mode` 选成 `publish` 或 `all`；本地：

```sh
export FANDOM_BOT_USER="MyAccount@MyBot"
export FANDOM_BOT_PASSWORD="..."
uv run wiki-translate --env .env --mode publish --dry-run
uv run wiki-translate --env .env --mode publish
uv run wiki-translate --env .env --mode all
```

### 增量推送

`state.json` 里每页维护两个 revid：

- `revid`：源 wiki 的版本（翻译阶段写入）
- `published_revid`：上次推送成功对应的版本（推送阶段写入）

仅在两者不一致时才会调用 `action=edit`，避免反复推送同一版本。

## GitHub Actions 三阶段架构

为了既打开"串行翻译 160+ 页"的 LLM 瓶颈，又避免对 wiki API 的高频读写触发限流，CI 把流程拆成 **fetch → process → upload** 三个阶段，对应 5 个相互依赖的 job：

```
setup ─► fetch ─► process (matrix: shard 0..N-1) ─► merge ─► upload
```

| Job | 阶段 | 作用 | 何时运行 |
|---|---|---|---|
| `setup` | — | 跑一次 `ruff check` + `pytest`，计算分片矩阵与各阶段开关 | 总是 |
| `fetch` | 拉取 | 单 job 集中读源 wiki（页面/文件/系统消息），产出 `manifest.json` + `cache/` | `mode=fetch/all` |
| `process` | 处理 | `strategy.matrix` 按 `shard_total` 并行，只做 LLM 翻译/代码复制，无 wiki API | `mode=process/all` |
| `merge` | 汇总 | 合并各分片 `output/cache/state delta`，统一 commit & push | 非 dry-run |
| `upload` | 上传 | 单 job 顺序推送页面/系统消息/文件到目标 wiki，集中控制频率 | `mode=upload/all` |

设计要点：

- **fetch 集中读**：所有源 wiki 读操作只在一个 job 里完成，避免多个分片各自重复枚举/抓取。
- **process 纯算力**：不触达任何 wiki API，因此可以放心地按分片高并发跑 LLM，互不影响也不会被 wiki 限流。
- **upload 集中写**：所有目标 wiki 写操作收敛到单 job 顺序执行，配合 `PUBLISH_SLEEP_BETWEEN` 严格控制写入频率，规避 API 限流与编辑冲突。

### 模板 / 代码的处理

fetch 阶段按标题给每个页面打 `action` 标签，process 阶段据此分流：

| 类型 | 命名空间 | action |
|---|---|---|
| 主条目 / 模板（`Template:`）/ 分类（`Category:`） | 0 / 10 / 14 | `translate`（走 LLM） |
| Lua 模块（`Module:`） | 828 | `copy`（原样复制，代码不翻译） |
| 站点脚本/样式（`.css` / `.js` / `.json`） | — | `copy` |

命名空间由 `.env` 的 `WIKI_NAMESPACES` 控制（如 `0,10,14,828`）。

### 分片

`process` 用 round-robin 切分（`i % shard_total == shard_index`）而非连续切片：相邻条目体量往往相近，轮转能让每个分片都拿到长短交错的条目，避免某个分片全是大页面拖慢整体。分片数由 `workflow_dispatch` 的 `shard_total` 输入控制，默认 4。

### state delta 合并

多个分片并发写同一份 `state.json` 会互相覆盖，因此引入 **delta 机制**：

- 每个分片设置 `STATE_DELTA_FILE`，运行时只把**本分片改动过的条目**（`pages` / `files` / `system_messages`）抽成一份独立的 delta state；
- `merge` job 下载所有 artifact，以仓库现有 `state.json` 为 base，按 section 逐个浅合并所有 delta（同名条目后者覆盖前者），再写回；
- 由 `--mode merge-state` 命令完成，读 `STATE_DELTA_DIR`（默认 `state-deltas`）目录下全部 `*.json`。

本地不设置 `STATE_DELTA_FILE` 时行为完全不变——直接 `uv run wiki-translate --mode all` 仍是单进程顺序执行 fetch+process+upload 并写整份 `state.json`。

### 相关环境变量

| 变量 | 含义 | 由谁设置 |
|---|---|---|
| `SHARD_INDEX` / `SHARD_TOTAL` | process 当前分片序号 / 总分片数 | matrix job 注入 |
| `STATE_DELTA_FILE` | 本 job 写 delta 的目标文件 | process / upload 注入 |
| `STATE_DELTA_DIR` | merge 阶段扫描 delta 的目录 | workflow 级 env |
| `STRATEGY_INCOMING_DIR` | fetch 写新源码、process 读的目录 | `.env`，默认 `cache/incoming` |
| `STRATEGY_MANIFEST_FILE` | 清单文件路径 | `.env`，默认 `manifest.json` |

> dry-run 时 fetch 只枚举与比对、不写 manifest，`process` / `merge` / `upload` 整段跳过，因此 `--dry-run` 不会产生任何提交或 wiki 改动。

## 开发

```sh
uv sync --group dev      # 安装含 ruff/pytest 的开发依赖
uv run ruff check src tests
uv run ruff format src tests
uv run pytest
uv run wiki-translate --help
```

CI（`.github/workflows/translate.yml`）会在每次运行时先跑 `ruff check` + `pytest`，失败直接中断流水线，避免把坏的脚本推到 cron 任务里。

## 术语表

`prompts/glossary.csv` 给定关键术语 → 译文映射，避免 LLM 在不同页面间忽然把 `Seek` 译成"求索者"或"寻找者"。

格式（首行必须是 header）：

```csv
en,zh,note
Seek,Seek,实体名保持英文
Crucifix,十字架,
Door,Door,
# 行首 # 是注释；空 zh 表示强制保持原样
```

注入规则：

- 启动时整张表加载到内存；
- 每次翻译/合并前，从当前 wikitext 里**整词大小写不敏感**搜索 `en` 列，仅命中的条目才注入 prompt（避免 token 浪费）；
- 注入数量超过 `TRANSLATOR_GLOSSARY_MAX_ENTRIES`（默认 50）时截断；
- 通过 `TRANSLATOR_GLOSSARY_FILE` 改路径；置为空字符串可禁用。

## 工作机制

本流水线实现的是 **三路 diff + 冲突感知** 的同步翻译：

```
源 Wiki ─┐                       ┌→ 推送目标 Wiki（先抓取做冲突检测）
        ├─ hash 比对 ─→ LLM 翻译 ┤
目标 Wiki─┘                       └→ commit 仓库（output/ + cache/ + state.json）
仓库现有译文 ──── 参考 ─────────┘
```

每个页面在 `state.json` 中维护四组指标：

| 字段 | 用途 |
|---|---|
| `source.{revid, hash, fetched_at}` | 上次从源 wiki 抓到的版本，用来跳过未变页面 |
| `target.{title, revid, hash, fetched_at}` | 推送阶段拉到的目标 wiki 现状（用于冲突检测） |
| `translation.{hash, file, translated_at}` | 仓库里译文文件的内容指纹 |
| `last_published.{target_revid, target_hash, translation_hash, published_at}` | 上次推送成功时，"目标 wiki" 与 "译文文件" 的 hash 快照 |

`cache/source/<title>.wiki` 缓存上次抓到的源 wikitext（提交进仓库），下次重新抓取后做 `unified_diff` 喂给 LLM，结合仓库现有译文一起作为翻译上下文，让模型保留术语与人工润色。

### 触发翻译的条件

| 情况 | 行为 |
|---|---|
| `source.hash` 没变且 `translation.hash` 已存在 | 跳过（除非 `--force`） |
| `source.hash` 变了 | 用 `cache/source/<title>` 与新源算 diff，连同仓库现有译文一起作为 LLM 输入，整页重译 |
| 仓库被人改过译文 | `translation.hash` 不变也没事，只要源没变就不重译；下次源更新时会把仓库译文当作 reference |
| `--force` | 忽略上述判断全量重译 |

### 推送时的冲突检测

每次推送前先 `action=query` 拉一次目标页面 → 算 hash → 与 `last_published.target_hash` 比对：

| 情况 | 行为 |
|---|---|
| 首次推送（无 `last_published`） | 直接推 |
| 目标 hash == 上次推送时的 hash | 安全推送，覆盖 |
| 目标 hash != 上次推送时的 hash | **目标 wiki 上有人工修订**，按 `PUBLISH_ON_TARGET_CONFLICT` 决定 |
| 目标 hash == 当前译文 hash | 跳过（已经一致） |

`PUBLISH_ON_TARGET_CONFLICT` 取值：

- `skip`（默认）：跳过该页并打 warning，避免覆盖人工修订
- `overwrite`：直接覆盖（也可用 `--force` 强制覆盖）
- `merge`：调 LLM 做三方合并（仓库译文 + 目标当前内容 + `cache/source` 上次源），输出兼顾更新与人工修订的新译文，写回 `output/` 后再推送。需要 `OPENAI_API_KEY`。

### Diff 上下文如何给 LLM

只有切块后的"第一块"会带上完整上下文（`old_source`/`source_diff`/`reference_translation`/`target_diff`）。后续块只发送当前块本身，避免每块都重复传 diff 把 token 耗光。System prompt 已强约束这些参考段落"只读不译"。

### Diff 级局部翻译（可选）

当 `STRATEGY_DIFF_TRANSLATION=true` 且同时具备以下条件时启用：

- `cache/source/<title>.wiki` 存在上次抓到的源 wikitext
- `output/<lang>/<title>.wiki` 存在上次的译文

工作机制：

1. 把源/旧源/旧译文按"空行"切成段落序列；
2. `difflib.SequenceMatcher` 在 (旧源, 新源) 上算 opcodes，标出每段是 `equal` 还是 `replace/insert/delete`；
3. **未变段直接复用旧译文**；变化段才调 `Translator.translate_segment()`，并把整页源/译文当作只读上下文喂给 LLM 保持术语一致；
4. 用 `stitch()` 按新源段落顺序拼回完整 wikitext。

回退保护：当**旧源段数 ≠ 旧译段数**（说明历史译文段落结构与源不对齐），自动回退到整页重译，避免错位。

收益：源 wiki 只改了一两段时，token 消耗 ≈ 1/N（N 为段数），cron 增量同步几乎免费。

dry-run 时会以 `[pending diff]` 展示规划：

```
[pending diff] revid=11976 字符=4179 段=23 复用=21 重译=2
```

## 切换到其它服务商

`TRANSLATOR_BASE_URL` 兼容 OpenAI Chat Completions 协议即可：

| 服务商 | TRANSLATOR_BASE_URL | TRANSLATOR_MODEL 示例 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 BigModel | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | `ep-xxxxxxxx` |

## 开发

```sh
uv sync --group dev      # 安装含 ruff 的开发依赖
uv run ruff check src    # 静态检查
uv run ruff format src   # 格式化
uv run wiki-translate --help
```

## 注意事项

- 遵守 Fandom 的 [API 使用条款](https://www.fandom.com/api-terms)，控制并发与请求频率
- `.env` 仅用于流水线配置，**不要写入真实密钥**；密钥统一走 GitHub Secrets / 真实环境变量
- LLM 翻译可能丢失少量罕见模板格式，建议人工抽样校对
- 启用 `PUBLISH_ENABLED=true` 会真实修改目标 wiki，第一次务必先 `--dry-run`
