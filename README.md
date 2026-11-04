# simple-codex-memory

按需启用的 Codex 项目记忆：**Markdown + SQLite + 一个小 CLI + 一个 skill**。

默认不启用，需要长期推进项目时手动调用；启用后由 Codex 在工作过程中更新。
不需要 embedding、额外模型、API key、MCP 或后台服务。Python 3.10+，零运行时依赖，
使用 Python 自带的 SQLite FTS5（Python 构建须启用 FTS5）。

## 安装

```bash
git clone https://github.com/Mifune-Shioriko/simple-codex-memory.git
cd simple-codex-memory
uv tool install .
memory install-skill
```

也可以用 `pipx install .` 替代 `uv tool install .`。
`install-skill` 只复制 skill 到 `~/.agents/skills/simple-codex-memory`，不会修改
`AGENTS.md` 或 Codex 全局配置。已有同名目录时拒绝覆盖。
可通过 `--dest /path/to/skills` 指定其他技能目录。

## 使用

在 Codex 中显式调用：

```text
$simple-codex-memory 为 /path/to/project 启用项目记忆。
先恢复进度，推进时维护关键决定和验证过的经验，阶段结束时整理下一步。
```

这个 skill 禁止隐式调用，设置在 `agents/openai.yaml`：

```yaml
policy:
  allow_implicit_invocation: false
```

启用范围是当前会话中的指定项目。新会话需要再次调用；也可以说“停止使用项目记忆”。
这是 skill 的行为约定，并非数据库权限隔离或 CLI 开关；停用不会移除已经读入当前
上下文的信息。技能名称和简介仍可能出现在 Codex 的技能列表中。

Codex 会直接编辑 Markdown，在里程碑、关键决定或重要发现后写回；更新能力依赖
模型遵循 skill，不保证每次都自动执行。必要时可以明确说“更新项目记忆”。

## CLI

始终使用明确的项目根目录；默认是当前目录，**不会向父目录寻找记忆**。
`--root` 放在子命令之前。

```bash
memory --root /path/to/project init
memory --root /path/to/project context
memory --root /path/to/project search "认证 令牌"
memory --root /path/to/project search "refresh token" --limit 3 --max-chars 4000
memory --root /path/to/project check
memory --root /path/to/project reindex
```

| 命令 | 作用 |
| --- | --- |
| `init` | 创建缺少的模板，保留已有文件 |
| `context` | 输出摘要和笔记列表，默认最多 6000 字符 |
| `search` | 按 Markdown 标题分段搜索，默认最多 5 个结果、共 6000 字符 |
| `check` | 检查必需文件与篇幅预算，不自动改写内容 |
| `reindex` | 从 Markdown 全量重建 SQLite 缓存 |
| `install-skill` | 安装只允许显式调用的 skill |

搜索先使用 FTS5 排序，再用子串匹配补充结果，支持中文短词。查询按普通关键词
处理，不接受 FTS 运算符；多个词需同时匹配。返回文件和段落起始行，较长段落会截断，
需要时按定位读取原文。这里没有语义检索、中文分词模型或跨语言匹配。

退出码：`0` 成功（包括搜索无结果），`1` 检查发现问题，`2` 命令或运行错误。

## 存储与约束

```text
project/.memory/
├── summary.md       # 目标、现状、阻碍、下一步
├── decisions.md     # 关键决定与原因
├── lessons.md       # 验证过的经验与依据
├── .gitignore       # 忽略数据库缓存
└── index.sqlite     # 按需生成，可删除重建
```

Markdown 是唯一事实来源。SQLite 只保存按标题切分的全文索引；`context` 和 `search`
按内容 hash 增量同步修改、新增与删除。可随时删除索引；数据库损坏时运行 `reindex`，
若 SQLite 无法打开损坏文件，先手动删除 `index.sqlite` 再运行。CLI 不负责摘要提炼，
由正在工作的 Codex 完成；离线运行 CLI 不会调用模型。

`summary.md` 上限为 100 行或 10000 字符，其他笔记每个 300 行或 30000 字符。
超限由 `check` 报告；Codex 应合并、修订、精简，必要时拆为主题文件。没有每日流水账，
也不自动导入历史会话。可自行增加 `.memory/**/*.md`，它们会参与索引。

存储在本地不等于内容始终不出本机：被 Codex 读取的内容会进入当前模型上下文。
不应存储凭据。CLI 不跟随记忆文件、目录或索引路径中的符号链接。

笔记可选择提交到项目 Git，索引默认忽略；如果笔记只供自己使用，在项目的
`.git/info/exclude` 添加 `/.memory/`。多个 Codex 同时编辑相同 Markdown 可能冲突，
第一版建议同一项目只保留一个记忆写入者。

## 开发与测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m simple_codex_memory --help
```

MIT License。
