# 人工抽检

不导入数据，先把接口做好。所有命令只在主人私聊生效。

## 可视化界面

```bash
.venv/bin/python scripts/eval_ui.py --port 8099
```

浏览器打开 `http://127.0.0.1:8099/`。

- 左边看上下文、触发消息、她的回复；
- 点标签直接提交并进入下一条，数字键 1~6 也可以；
- 备注可选；
- 右边看进度、标签分布、最近评过；
- 可随时导出 CSV；
- 右上角 **“结束服务并训练”**：停止 8099 服务，生成 `data/eval/training-report.md` 和 `data/eval/preferences.jsonl`。这里的“训练”是反馈校准/偏好数据，不是微调模型权重；39 条也不足以做可靠 fine-tune。

界面只绑定 `127.0.0.1`，不要把它暴露到公网。

## 每周流程

```text
/评测 采集 7        # 从最近 7 天群聊里采样 40 条她回过的消息
/评测               # 看下一条
/评 像人            # 记录，可加备注：/评 冷淡 回得太短
/评测 统计           # 看整体标签分布
/评测 导出           # 导出 data/eval/labels.csv
```

## 标签

| 标签 | 含义 |
|---|---|
| `像人` | 时机、内容、语气都自然 |
| `还行` | 没大问题，但不算很像 |
| `冷淡` | 太短、太敷衍、接得生硬 |
| `不懂装懂` | 不掌握话题中心还硬发表意见 |
| `不该回` | 这条根本不该接 |
| `跳过` | 这条不适合评 |

## 报表

```bash
.venv/bin/python scripts/eval_report.py --days 7
.venv/bin/python scripts/eval_report.py --days 7 --out data/eval/report.md
```

报表包含：

- 人工抽检：总用例、待评、像人率、明显问题率、标签分布；
- 线上：群数、群员消息数、她发言数、被质疑像 AI 次数、被指出前后不一致次数；
- 最近的问题用例。

## CSV

`data/eval/labels.csv` 可直接用 Excel 打开，列：

```text
id, gid, uid, nickname, case_type, trigger, reply, status,
label, should_reply, note, created_at
```

## 后续可加

- LLM 预评分：先让模型按同样标签打一遍，再和人工结果对比；
- 每周自动生成报表；
- 从“她没回”的消息里采样，专门评“该接没接”。
