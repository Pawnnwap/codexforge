# 守门案例：httpx

- 来源：https://github.com/encode/httpx
- license：BSD-3-Clause（实测 `LICENSE.md`）
- commit：`b5addb64f016`（2026-02-23T10:40:42Z）
- 评估范围：`httpx`
- 选它的理由：工业级 HTTP 客户端，注释与类型密度高，可检验度量在高成熟度代码上的假阳性
- 运行时间：2026-10-26 21:56:27

## 结果

| 指标 | 基线 | 守门后 | 变化 |
|---|---|---|---|
| 整洁度分数 | 80.5 | 80.5 | +0.0 |
| 问题数 | 19 | 19 | +0 |
| high 级问题 | 1 | 1 | +0 |
| SLOC | 7091 | — | — |
| 文件数 | 23 | — | — |

耗时 205.4s（scripted 修复器，零 token）。

## 修复器处置

- 第 1 轮：执行，判定 **回滚**，分数 80.5 → 80.5，终止原因：连续 1 轮改善不足，判定收敛

## 问题类型分布（基线）

| 类型 | 数量 |
|---|---|
| `long_function` | 8 |
| `dead_assignment` | 5 |
| `deep_nesting` | 3 |
| `duplicate_branch` | 2 |
| `todo_marker` | 1 |

## 剩余债务（检测到但 scripted 修不了）

> 这一节必须保留。分数上涨不等于代码干净 —— scripted 修复器只处理「未使用 import」这类可确定性删除的问题；裸 except、深嵌套、跨文件重复都需要 codex 修复器或人工。把它们隐去等于虚报。

| 类型 | 位置 | 严重度 | 摘要 |
|---|---|---|---|
| `long_function` | `_api.py:39` | medium | request() 有 82 行（阈值 60） |
| `todo_marker` | `_auth.py:267` | low | 遗留标记：# TODO: implement auth-int |
| `long_function` | `_client.py:639` | medium | __init__() 有 78 行（阈值 60） |
| `long_function` | `_client.py:1353` | medium | __init__() 有 78 行（阈值 60） |
| `long_function` | `_main.py:26` | medium | print_help() 有 75 行（阈值 60） |
| `deep_nesting` | `_main.py:0` | medium | 最大嵌套 7 层（阈值 4） |
| `deep_nesting` | `_models.py:0` | medium | 最大嵌套 5 层（阈值 4） |
| `long_function` | `_transports/asgi.py:99` | medium | handle_async_request() 有 89 行（阈值 60） |
| `dead_assignment` | `_transports/asgi.py:154` | low | send() 内有赋值后未使用的变量 |
| `dead_assignment` | `_transports/asgi.py:155` | low | send() 内有赋值后未使用的变量 |
| `long_function` | `_transports/default.py:136` | medium | __init__() 有 80 行（阈值 60） |
| `long_function` | `_transports/default.py:280` | medium | __init__() 有 80 行（阈值 60） |

## 结论与边界（必读）

本轮无净改善（+0.0）。详见上方逐轮处置与剩余债务两节。

## 可复现

```bash
python experiments/run_case.py --repo httpx
```

scripted 修复器行为确定，同一 commit 重跑得到同一结果；
codex 修复器受上游模型状态影响，不用于可核验的对比数据。
