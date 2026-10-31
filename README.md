# qqbot-control

一个 QQ 群聊 agent 的**控制面**仓库：人格、规则、Canon、评测场景、变更流程。

运行时（QQ 登录、数据库、日志、群聊记忆）留在服务器本地，不进这个仓库。

## 设计

```text
GitHub（本仓库）        本地工作台               Bot Runtime
policy / canon / PR  →  workbench  →  control 快照  →  bot
评测场景 / CI 校验       指标 / 评测                群聊 / 记忆
```

- Git 负责版本、评审、回滚；
- 工作台负责日常操作；
- bot 只读本地控制快照，不依赖 GitHub 在线。

## 目录

```text
bot/              bot 源码
scripts/          运行、评测、工作台脚本
eval_ui/          人工评测页面
workbench_ui/     管理人工作台
control/          policy.json / release.json
canon/            结构化知识条目
eval/scenarios/   固定回归场景
persona/          可公开的人格方案与补丁说明
docs/             评测、语料、工作流说明
```

## 本地启动

```bash
cp config/.env.example config/.env
# 填入 DEEPSEEK_API_KEY、MASTER_UIDS 等
python -m venv .venv
.venv/bin/pip install -r requirements.txt  # 如果存在
.venv/bin/python scripts/workbench.py --port 8099
```

浏览器打开 `http://127.0.0.1:8099/`。评测页在 `/eval`。

工作台里的 **GitHub** 页可以：

- 提交 `control/` 到新分支；
- 创建 PR；
- 轮询 `validate` 检查；
- 通过后点“合并并发布”；
- 自动打 `control-vN` tag，并以 GitHub 上的 policy 写回本地。

需要在 `config/.env` 配置 `GH_REPO`、`GH_TOKEN`（fine-grained PAT，最小权限）。

> 这个项目依赖本地 QQ / NapCat 环境，不是开箱即用的 SaaS。

## 控制规则

`control/policy.json`：

```json
{
  "version": 1,
  "rules": [
    {"id": "group.silence.emotion", "scope": "group", "priority": 100,
     "text": "别人只回哈哈哈、没有新信息时，不接。", "status": "active"}
  ]
}
```

- 规则只追加，不覆盖人格核心；
- 发布后 bot 下一条消息读取新版本；
- 冲突时以人格核心为准。

## GitHub 工作流

```text
改 control → 分支 → PR → validate → 合并 → tag control-vN → bot 拉取
```

`validate` 会检查：

- `policy.json` / `release.json` 结构；
- 规则 id 唯一、字段合法；
- 仓库里没有明显的密钥。

## 安全

- 不要提交 `.env`、数据库、日志、群聊原文、成员画像；
- Canon 只存摘要和短引文；
- 工作台只绑定 `127.0.0.1`；
- GitHub token 用 fine-grained PAT，最小权限；
- 生产拉取用只读 deploy key。

## License

TBD.
