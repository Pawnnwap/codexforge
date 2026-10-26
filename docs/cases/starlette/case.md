# 守门案例：starlette

- 来源：https://github.com/encode/starlette
- license：BSD-3-Clause（实测 `LICENSE.md`）
- commit：`8aa828ceaff7`（2026-10-24T07:58:54-07:00）
- 评估范围：`starlette`
- 选它的理由：ASGI 框架核心，抽象层多，适合观察跨文件重复的误报率
- 运行时间：2026-10-26 22:03:53

## 结果

| 指标 | 基线 | 守门后 | 变化 |
|---|---|---|---|
| 整洁度分数 | 87.8 | 87.8 | +0.0 |
| 问题数 | 28 | 28 | +0 |
| high 级问题 | 1 | 1 | +0 |
| SLOC | 5691 | — | — |
| 文件数 | 36 | — | — |

耗时 343.1s（scripted 修复器，零 token）。

## 修复器处置

- 第 1 轮：执行，判定 **回滚**，分数 87.8 → 87.8，终止原因：连续 1 轮改善不足，判定收敛

## 问题类型分布（基线）

| 类型 | 数量 |
|---|---|
| `long_function` | 8 |
| `dead_assignment` | 5 |
| `deep_nesting` | 5 |
| `duplicate_branch` | 4 |
| `long_line` | 3 |
| `todo_marker` | 3 |

## 剩余债务（检测到但 scripted 修不了）

> 这一节必须保留。分数上涨不等于代码干净 —— scripted 修复器只处理「未使用 import」这类可确定性删除的问题；裸 except、深嵌套、跨文件重复都需要 codex 修复器或人工。把它们隐去等于虚报。

| 类型 | 位置 | 严重度 | 摘要 |
|---|---|---|---|
| `dead_assignment` | `_exception_handler.py:38` | low | sender() 内有赋值后未使用的变量 |
| `long_function` | `authentication.py:25` | medium | requires() 有 74 行（阈值 60） |
| `long_function` | `authentication.py:32` | medium | decorator() 有 65 行（阈值 60） |
| `long_function` | `middleware/base.py:101` | medium | __call__() 有 108 行（阈值 60） |
| `long_function` | `middleware/base.py:112` | medium | call_next() 有 87 行（阈值 60） |
| `dead_assignment` | `middleware/base.py:169` | low | call_next() 内有赋值后未使用的变量 |
| `dead_assignment` | `middleware/base.py:151` | low | coro() 内有赋值后未使用的变量 |
| `duplicate_branch` | `middleware/cors.py:163` | medium | if/elif 分支体完全相同，条件可能写错或可合并 |
| `long_line` | `middleware/cors.py:48` | low | 第 48 行 158 字符（阈值 120） |
| `dead_assignment` | `middleware/errors.py:160` | low | _send() 内有赋值后未使用的变量 |
| `long_line` | `middleware/errors.py:110` | low | 第 110 行 128 字符（阈值 120） |
| `todo_marker` | `middleware/exceptions.py:26` | low | 遗留标记：self.debug = debug  # TODO: We ought to handle 404 case |

## 结论与边界（必读）

本轮无净改善（+0.0）。详见上方逐轮处置与剩余债务两节。

## 可复现

```bash
python experiments/run_case.py --repo starlette
```

scripted 修复器行为确定，同一 commit 重跑得到同一结果；
codex 修复器受上游模型状态影响，不用于可核验的对比数据。
