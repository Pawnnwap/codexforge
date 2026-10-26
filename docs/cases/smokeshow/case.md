# 守门案例：smokeshow

- 来源：https://github.com/samuelcolvin/smokeshow
- license：MIT（实测 `LICENSE`）
- commit：`6e982a3d08dc`（2025-01-07T21:00:54Z）
- 评估范围：`.`
- 选它的理由：中等规模 CLI 工具，含 CLI/服务/存储多层，典型「个人项目长期演进」形态
- 运行时间：2026-10-26 21:11:13

## 结果

| 指标 | 基线 | 守门后 | 变化 |
|---|---|---|---|
| 整洁度分数 | 95.2 | 97.3 | +2.1 |
| 问题数 | 6 | 5 | -1 |
| high 级问题 | 0 | 0 | +0 |
| SLOC | 509 | — | — |
| 文件数 | 9 | — | — |

耗时 306.9s（scripted 修复器，零 token）。

## 修复器处置

- 第 1 轮：执行，判定 **接受**，分数 95.2 → 97.3，终止原因：达到最大轮数 1

## 问题类型分布（基线）

| 类型 | 数量 |
|---|---|
| `todo_marker` | 5 |
| `long_function` | 1 |

## 剩余债务（检测到但 scripted 修不了）

> 这一节必须保留。分数上涨不等于代码干净 —— scripted 修复器只处理「未使用 import」这类可确定性删除的问题；裸 except、深嵌套、跨文件重复都需要 codex 修复器或人工。把它们隐去等于虚报。

| 类型 | 位置 | 严重度 | 摘要 |
|---|---|---|---|
| `todo_marker` | `cli/tests/test_gh_integration.py:65` | low | 遗留标记：env.set('SMOKESHOW_GITHUB_TOKEN', 'xxx') |
| `todo_marker` | `cli/tests/test_upload.py:27` | low | 遗留标记：(tmp_path / 'bar.unknown_extension').write_text('xxx') |
| `todo_marker` | `cli/tests/test_upload.py:43` | low | 遗留标记：'body': b'xxx', |
| `todo_marker` | `cli/tests/test_upload.py:55` | low | 遗留标记：(tmp_path / 'bar.unknown_extension').write_text('xxx') |
| `todo_marker` | `cli/tests/test_upload.py:67` | low | 遗留标记：(tmp_path / 'bar.unknown_extension').write_text('xxx') |

## 结论与边界（必读）

**接受修复，净改善 +2.1 分**，剩余 5 个问题属修复器能力边界外的债务。

## 可复现

```bash
python experiments/run_case.py --repo smokeshow
```

scripted 修复器行为确定，同一 commit 重跑得到同一结果；
codex 修复器受上游模型状态影响，不用于可核验的对比数据。
