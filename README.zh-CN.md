# ghostmerge

[![ci](https://github.com/wujiayue19160200073/ghostmerge/actions/workflows/ci.yml/badge.svg)](https://github.com/wujiayue19160200073/ghostmerge/actions/workflows/ci.yml)

**git 说这个合并是干净的。测试说它 `ImportError`。**

`ghostmerge` 找出那些 git 判定为“零冲突”、但合并之后一跑就崩的合并。方法是**真的把合并结果跑一遍**：在临时工作树里，用你自己的测试命令，而不是对着 diff 猜。

> 终端输出是英文（这个工具面向 GitHub 全球用户），下面是原样输出，没有改写。

```
$ ghostmerge check --all --test "pytest -q"

  ghostmerge  3 branches · 3 pairs · base main

  1 ghost merge

  ✗ agent/auth-refactor × agent/reports
    merge  clean    git saw no conflict at all
    tests  FAILED   exit 1

    │   File "tests/test_reports.py", line 3, in <module>
    │     from reports import build_report
    │   File "reports.py", line 1, in <module>
    │     from users import get_user
    │ ImportError: cannot import name 'get_user' from 'users'. Did you mean: 'fetch_user'?

    what broke it
      agent/auth-refactor deleted get_user, renamed to fetch_user  (users.py:6)
        -def get_user(user_id):
      agent/reports still calls it  (reports.py:1)
        +from users import get_user

  ── all pairs ────────────────────────────────────────
  ✓ agent/auth-refactor × agent/pagination  ·  pass   ·  1.2s
  ✗ agent/auth-refactor × agent/reports      ·  GHOST  ·  1.2s
  ✓ agent/pagination × agent/reports         ·  pass   ·  1.5s

  git says 0 conflicts. Your tests say 1.
  Each branch is green on its own; the break exists only after the merge.
```

## 这个工具是为了哪种情况

分支 A 把 `get_user` 改名成 `fetch_user`，并且更新了它能看到的每一处调用。它的测试是绿的。

分支 B 新增了一个文件，里面 import 了 `get_user`。它的测试也是绿的——因为在分支 B 上，这个函数还在。

两条分支改的是**不同文件的不同行**，git 合起来一个冲突标记都不会有。合并结果能编译、能 import，然后在你第一次真正调用时抛 `ImportError`——三天后，线上，在两个作者都没打开过的代码里。

这不是边缘情况，这是常态：

- 两个 Agent 在各自的工作树里并行干活，单独看都是绿的
- 一条长命的重构分支，加上一周的正常需求
- 有人正在基于旧名字写新代码，同时改名或者改 schema 的改动落地了

你的测试是唯一能抓住它的东西，而你的测试**从来没在合并后的代码上跑过**。现在跑过了。

## 一条命令先看效果

```bash
pipx install git+https://github.com/wujiayue19160200073/ghostmerge
ghostmerge demo
```

`demo` 会在临时目录里造一个真仓库——三个分支、一次改名、一个新调用——然后当场检查给你看。不需要配置，也不需要你已经有个项目。上面那段输出就是它跑出来的。

## 安装

```bash
# 推荐：隔离环境，装完直接在 PATH 上
pipx install git+https://github.com/wujiayue19160200073/ghostmerge

# 或者装进当前环境
pip install git+https://github.com/wujiayue19160200073/ghostmerge

# 想先看代码再装
git clone https://github.com/wujiayue19160200073/ghostmerge
cd ghostmerge && pip install -e .
```

还没发 PyPI——名字留着了，但发布是另一步，我不想让 README 里第一条命令是假的。

需要 **git ≥ 2.38**（`git merge-tree --write-tree`）和 **Python ≥ 3.9**。零依赖、不联网、不上报任何数据。

## 用法

### 检查你点名的分支

```bash
ghostmerge check feature-a feature-b --test "pytest -q"
```

你给的每两个分支之间都会做一次真合并并跑测试。

### 检查你的 Agent 在干什么（主要场景）

```bash
ghostmerge check --worktrees --test "pytest -q"
```

取所有工作树上检出的分支（除 base 外），两两配对检查。2–6 个并行 Agent 是它的舒适区，再多请看[代价](#代价)。

### 检查所有还没合并的分支

```bash
ghostmerge check --all --test "pytest -q"
```

配对数是 `n(n-1)/2`，分支多的时候要有意识地用。

### 接进 CI 或 hook

```bash
ghostmerge check --all --test "pytest -q" --markdown > ghost.md   # 贴 PR 评论
ghostmerge check --all --test "pytest -q" --json                  # 给机器读
ghostmerge check --all --test "pytest -q" --no-fail               # 只提示，不阻断
```

退出码：`0` 没有幽灵合并 · `1` 至少一个 · `2` 用法或环境有问题。

### 其它命令

```bash
ghostmerge doctor    # git 版本够不够、测什么命令、要跑多少对
ghostmerge clean     # 清掉崩溃残留的临时工作树
```

配置放仓库根的 `.ghostmerge.json`：

```json
{ "base": "main", "test": "pytest -q", "timeout": 600 }
```

## 它到底做了什么

1. `git merge-tree --write-tree` 在**对象库里**算出合并树。不 checkout、不动 index，你的工作目录全程不被碰。
2. 这棵树被包成一个游离的、不可达的 commit。
3. 它被检出到**仓库之外**的临时工作树（系统临时目录），所以 linter、测试发现机制、`git status` 都不会误入。
4. 你的测试命令在那里跑。
5. 工作树在 `finally` 里删掉——包括你按 Ctrl-C 的时候。
6. 如果合并是干净的、测试却挂了，这一对被记为**幽灵合并**，并指出是哪条分支删了符号、哪条还在调用。

清理这件事是靠测试保证的，不是靠“应该没问题”：有一个测试专门断言跑完之后 `git worktree list` 只剩一条记录。

## 为什么不做静态分析

这个问题的静态解法已经有人占了：解析 diff、抽符号足迹、当一条分支删掉的名字被另一条分支最近用过时告警。它需要每种语言一个解析器，而且它最多只能说“**可能会坏，请自行确认**”。

`ghostmerge` 说的是“**它坏了，堆栈在这**”——因为合并后的代码就在那儿，跑一遍的代价只是一个工作树。

这个取舍说清楚：静态方案更快、不需要测试。**如果你没有测试，它比我合适。**如果你有测试，跑一遍才是真相。

## 代价

每一对等于一次完整测试。3 个分支 3 次，5 个分支 10 次，10 个分支 45 次。

- 用 `--base` 收窄范围，只查真正在飞的分支，不要查仓库里所有分支。
- `--timeout`（默认 600 秒）防止挂住的测试拖死整轮；超时会被记为 `timeout`，**永远不会被算成幽灵合并**。
- `--jobs N` 并发跑配对，默认是 `1`，故意的：两个测试同时跑会抢端口和数据库，而这正是本工具要抓的那类故障。真要提高，先给每个测试套自己的端口和库。
- CI 里定时跑或合并前跑，别每次 push 都跑。

## 它不做什么

- **不合并、不推送任何东西。** 不建分支、不移动分支、不删分支。试验用的 commit 不可达，会被回收。
- **不替代 merge queue。** 它回答“这两条分支能不能合在一起”，不是“main 绿不绿”。
- **证不出来就不猜。** 测试输出里没有符号名，它直接说没归因，收工。不会编一个“置信度 87%”来填满界面。
- **不读你的测试结果。** 它跑你给的命令、看退出码。要是命令写成 `true`，所有合并都是通过的——这怪命令，不怪工具。

## 环境

- git ≥ 2.38，Python ≥ 3.9
- Linux / macOS / Windows，三个平台都在 CI 里测
- 零 Python 依赖

## 许可

MIT。
