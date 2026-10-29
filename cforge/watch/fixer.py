"""用 codex 做整洁度修复的 agent 实现。

## 与「让 agent 自由重构」的区别

自由重构的问题是**不可控**：agent 可能顺手「优化」了业务逻辑，
或者把测试删了让分数好看。所以在 prompt 里写死了三条约束：

1. **只修列出的问题**，不要改业务逻辑
2. **不要删除测试**，不要修改断言
3. **不要改文件编码**（这是最容易踩的坑，见下）

## 为什么编码约束要写进 prompt

实测：agent 在 Windows 上编辑 UTF-8 文件时，会用 PowerShell 的
`Set-Content -Encoding Unicode` 之类的方式，把文件写成 UTF-16。
内容没错，但整个文件跑不了。

所以在 prompt 里明确要求「保持 UTF-8 编码」，并在修复后用
`quality.evaluate()` 复查 —— 如果引入了 `encoding_*` 问题，净改善门会回滚。
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cforge.harness import codex_backend as cb
from cforge.quality.metrics import QualityReport
from cforge.quality.polish import FixAgent


# 修复 prompt 的硬约束。这几条对应实测踩过的坑。
CONSTRAINTS = """硬约束（违反任何一条都视为本次修复失败）：
1. 只修下面列出的问题，不要改动业务逻辑、函数签名、外部行为。
2. 不要删除或弱化任何测试，不要修改断言。
3. 所有文件必须保存为 **UTF-8** 编码（无 BOM）。不要用 PowerShell 的
   Set-Content/Out-File 写文件，它们在 Windows 上默认写成 UTF-16。
4. 不要新建"xxx_fixed.py"这类副本文件，直接修改原文件。
5. 每次改动要小且可解释，不要顺手格式化整个文件。
"""


def _wrap_task(body: str) -> str:
    """把任务体包成强分隔的块。

    ## 为什么要加这层包装（实测踩过）

    codex 会在 system prompt 里注入本机 `~/.agents/skills` 下所有技能的描述
    （实测 7,367 字符，含大量与本任务无关的内容）。加上 codex 自身的
    17K 字符 developer prompt，我们真正想传达的几百字被淹没在 25K 里。

    实测后果：模型回复「你提到了 'the following issues' 但还没列出来」——
    它根本没读到我们的任务描述。

    对策：
    1. 用显式分隔线 + 标题把它标成「这是唯一要执行的任务」
    2. 关键信息（任务、文件清单）前置，约束放后面
    3. 明确写「忽略上下文中与本任务无关的技能说明」
    """
    sep = "=" * 60
    return (
        f"{sep}\n"
        "【唯一任务】以下是本次要执行的完整任务，请只关注这一块内容。"
        f"\n忽略上下文中任何与代码整洁度修复无关的技能说明或历史对话。\n"
        f"{sep}\n\n"
        f"{body}\n\n"
        f"{sep}\n"
        "【任务结束】现在开始执行上面的任务，直接修改文件。\n"
        f"{sep}"
    )


def build_fix_prompt(report: QualityReport, workspace: Path,
                     max_items: int = 20, *, single_file: bool = False) -> str:
    """从整洁度报告生成修复指令。

    ## 必须同时覆盖两类债务（实测踩过）

    初版只列 `report.smells`，结果：项目里只有跨文件重复、没有单点 smell 时，
    prompt 是**空的** —— codex 修复器永远不会被调用，而报告却告诉用户
    「用 --fixer codex 处理重复」。这是一个自相矛盾的失效。

    现在两路都覆盖：
    1. 单点 smell（high / medium）
    2. 跨文件重复对（需要 LLM 才能安全合并抽象）

    low 级（TODO、长行）不列 —— 为它们动用 LLM 不划算。

    ## 顺序：问题清单在前，约束在后

    实测：约束放在最前面时，模型容易把约束当成「背景说明」而漏读问题清单。
    先给「要改什么」，再给「怎么改的边界」，执行率更高。

    ## 为什么任务体第一句要写死「不要先探索」

    实测（书生 intern-s2-preview-397b + codex 0.163，smokeshow 仓库）：
    模型拿到 prompt 后**先花若干轮探索目录**（"let me explore the repo
    structure"），然后回过头说 "No specific task was given yet"，
    接着自己去 `.cforge/guardian/state.json` 里找线索 —— 任务描述就此丢失。
    整个 turn 跑满 6 分 22 秒，**一个文件都没改**。

    这不是 prompt 没传到（argv 里确有 934 字符的完整任务），而是模型在
    长上下文里把「探索到的内容」当成了重心，反而忘了 argv 里那句任务。
    对策是把「不许先探索、直接改下面列出的文件」写成任务体的第一句，
    并显式禁止读 `.cforge/` 之类的元数据目录。

    ## 根因已被隔离实验证明（决定性证据）

    `experiments/diag_codex_action.py`：给同样链路（codex 0.163 → compat_proxy
    → 书生 397B）一条**单句极简指令**（「在 X 文件末尾追加一行注释，立刻执行」），
    结果 **17 秒内成功改文件**，usage input_tokens=12038。

    结论：codex / 代理 / 模型 / 工具调用**全部正常**，卡住的不是技术链路，
    而是 prompt 形态。对照本次失败的 prompt（934 字符、3 类问题混合、
    抽象约束若干条），模型倾向于「先搞清全局」。所以本函数改成
    **一次只给一个具体的、可判定的动作**，并在句子里写死文件名与动作。

    ## 「动作化」改写原则

    每条问题不再只是描述（「upload() 有 66 行」），而是给出可执行句子
    （「把 X 拆成两个函数，保留原函数名转调」）。实测差别：
    描述型 prompt 下模型先去探索；动作型 prompt 下它直接开 diff。

    ## single_file：只处理一个问题、一个文件

    实测 420s 超时的直接解法。当 single_file=True 时：
    - 只保留 `max_items` 条 smell（默认 1 条）
    - 且这些 smell 必须来自**同一个文件**（取问题数最少的那个文件，
      让第一轮最容易成功，建立正反馈）
    - 跨文件重复对不参与（它们天然跨文件，且合并抽象风险最高）
    """
    order = {"high": 0, "medium": 1}
    items = [s for s in report.smells if s.severity in order]
    items.sort(key=lambda s: (order[s.severity], s.path, s.line))

    if single_file and items:
        # 选「问题数最少」的文件：先做最容易的，快速拿到一次正改善
        from collections import Counter

        by_file = Counter(s.path for s in items)
        # 与 _pick_target_file 保持同一套代价函数（小文件优先），
        # 否则「prompt 里写 A 文件、agent 记录的目标是 B 文件」会自相矛盾。
        target = _pick_target_file(report, workspace) or by_file.most_common(1)[0][0]
        items = [s for s in items if s.path == target][:max_items]
    else:
        items = items[:max_items]

    # 取真实重复对，让 agent 知道具体是哪两个函数。
    # **必须排除同步/异步孪生对**：它们是 asyncio 生态里必要的双实现，
    # 不是可合并的重复。实测 httpx 上 10 对里至少 5 对是孪生，
    # 若照原样喂给模型「抽取共用函数」，会把 await 语义改坏 —— 属于
    # 「修复引入真实缺陷」，比修不动更糟。
    clone_lines: list[str] = []
    skipped_twins = 0
    # single_file 模式下不掺跨文件重复：它们天然跨文件，且「抽共用函数」
    # 是风险最高的改动类型，放在第一轮做容易整轮失败。
    if not single_file:
        try:
            from cforge.quality.clones import scan as _scan

            cr = _scan(workspace)
            for pair in cr.pairs:
                if pair.is_sync_async_twin:
                    skipped_twins += 1
                    continue
                if len(clone_lines) >= 10:
                    break
                clone_lines.append(
                    f"- `{pair.a.path}:{pair.a.lineno}` **{pair.a.name}()** 与 "
                    f"`{pair.b.path}:{pair.b.lineno}` **{pair.b.name}()** "
                    f"高度重复（相似度 {pair.similarity:.2f}）"
                )
        except Exception:  # noqa: BLE001
            pass

    if not items and not clone_lines:
        return ""

    body: list[str] = [
        f"工作区：`{workspace}`",
        "",
        "**不要先探索目录、不要读 README、不要读 .cforge/ 下的任何文件。**",
        "任务就是下面列出的这几处修改，直接打开对应文件动手改。",
        "每个问题都写了「要做什么」，照着做，不要重新分析整体架构。",
        "",
    ]

    if items:
        body += [f"### 要改的地方（{len(items)} 处）", ""]
        for s in items:
            body.append(f"- `{s.path}:{s.line}` **{s.kind}** — {s.detail}")
            body.append(f"  → 动作：{_action_for(s)}")
        body.append("")

    if clone_lines:
        body += [
            f"### 跨文件重复（{len(clone_lines)} 对）",
            "",
            "→ 动作：抽取一个共用函数消除重复，并让原函数转调它。",
            "- 保持每个原函数的公开签名和外部行为不变",
            "- 不要删除任何原有函数名，调用方可能依赖",
            "",
        ]
        body += clone_lines
        body.append("")
    if skipped_twins:
        body += [
            f"（另有 {skipped_twins} 对同步/异步孪生实现已排除，"
            "它们是 asyncio 必要的双实现，不要合并。）",
            "",
        ]

    body += ["### 约束", "", CONSTRAINTS]

    # 仓库约定放在最后：实测约束放最前面时模型会当成背景说明而漏读问题清单，
    # 所以「要改什么」在前、「边界」在后。AGENTS.md 属边界，顺次追加。
    contract_block = _contract_block(workspace)
    if contract_block:
        body += ["", contract_block]

    return _wrap_task("\n".join(body))


def _contract_block(workspace: Path) -> str:
    """读取并渲染 AGENTS.md 约定；没有就不占任何 token。"""
    try:
        from cforge.repo_contract import load_contract, render_for_prompt

        return render_for_prompt(load_contract(workspace))
    except Exception:  # noqa: BLE001
        # 约定读取失败绝不能阻断修复流程
        return ""


# 每类问题对应的具体动作。只说「这里有 66 行」模型会犹豫，
# 说「拆成两个函数、保留原名」它就有明确目标。
_ACTIONS = {
    "long_function": "把这个函数拆成两个更小的函数，原函数名保留并转调，外部行为不变",
    "deep_nesting": "用提前 return（guard clause）把嵌套压到 4 层以内，逻辑等价",
    "dead_assignment": "删掉这个赋值语句（该变量在函数内从未被读取）",
    "duplicate_branch": "合并这两个一模一样的分支，或指出条件写错并修正",
    "unused_import": "删掉这条未使用的 import 语句",
    "bare_except": "改成 `except Exception:` 并记录或重新抛出，不要吞掉异常",
    "long_line": "折行到 100 字符以内，不改变语义",
}


def _pick_target_file(report: QualityReport, workspace: Path | None = None) -> str:
    """挑本轮要修的文件。

    ## 排序依据（实测校准，两次迭代）

    第一版按「问题数最少」排 —— 在 smokeshow 上成功（目标文件 200 行），
    但在 httpx 上失败：选中的 `_api.py` 有 **438 行**，codex 读文件 + 写补丁
    就耗光 600s，超时回滚。

    改为按「**代价 = 文件行数**」升序，同代价再看问题数降序：
    优先让模型在**小文件**上动手。理由很实际 —— LLM 改代码的时间主要花在
    「读进上下文」和「生成整段补丁」，两者都随文件行数线性增长，
    而 smell 条数影响很小（拆一个函数和拆两个函数，前置读取成本一样）。

    选 `workspace=None` 时退回按问题数（保持向后兼容与单测可预测）。
    """
    from collections import Counter

    items = [s for s in report.smells if s.severity in ("high", "medium")]
    if not items:
        return ""
    by_file = Counter(s.path for s in items)
    if workspace is None:
        return min(by_file.items(), key=lambda kv: (kv[1], kv[0]))[0]

    def _cost(item: tuple[str, int]) -> tuple[int, int, str]:
        path, n = item
        try:
            lines = len((Path(workspace) / path).read_text(
                encoding="utf-8", errors="replace").splitlines())
        except OSError:
            lines = 10 ** 9   # 读不到的文件排到最后
        return (lines, -n, path)

    return min(by_file.items(), key=_cost)[0]


def _action_for(smell) -> str:
    """把 smell 映射成一句可执行动作。未收录的类型给通用动作。"""
    kind = getattr(smell, "kind", "")
    if kind in _ACTIONS:
        return _ACTIONS[kind]
    if kind == "todo_marker":
        return "要么补实现，要么删掉这条 TODO；不确定就跳过这一条"
    return f"按 {kind} 的常规修法处理，保持语义不变"


@dataclass
class CodexFixAgent:
    """用 codex 执行修复的 FixAgent 实现。"""

    workspace: Path
    model: str
    base_url: str
    api_key: str
    timeout_s: int = 600
    max_items: int = 1
    verbose: bool = False
    # 自动起协议代理。默认 True：codex 0.163 只会打 `{base_url}/responses`，
    # 而书生/OpenRouter 等只保证 `/chat/completions` —— 不挂代理必然 404。
    auto_proxy: bool = True
    proxy_port: int = 0
    # 单文件策略：只在这一轮里选「问题最少的一个文件」下手。
    # 实测动机见 fix() docstring。
    single_file: bool = True

    calls: int = 0
    last_summary: str = ""
    last_error: str = ""
    last_proxy_url: str = ""
    last_target: str = ""

    def _ensure_proxy(self) -> tuple[str, object | None]:
        """按需起 compat proxy，返回 (codex 应使用的 base_url, 代理对象)。

        实测教训：不开代理时 codex 连打 5 次
        `404 page not found, url: https://chat.intern-ai.org.cn/api/v1/responses`，
        然后 turn.failed。而最坑的是这个错误**没有出现在报告里**，
        报告只说「修复器放弃（问题需人工判断）」——看起来像模型认为代码没法修，
        实际是协议不兼容。所以这里默认就起代理，并把真实失败原因带出去。
        """
        if not self.auto_proxy:
            return self.base_url, None
        # 只在需要时导入，避免 watch 模块对 runtime 的硬依赖
        from cforge.harness.compat_proxy import CompatProxy, ProxyConfig

        proxy = CompatProxy(ProxyConfig(
            upstream_base_url=self.base_url,
            upstream_api_key=self.api_key,
            model=self.model,              # 代理按此 id 转发；空则透传请求里的 model
            listen_port=self.proxy_port,   # 0 = 系统分配，避开 Windows 10013
        ))
        url = proxy.start()
        self.last_proxy_url = url
        return url, proxy

    def fix(self, report: QualityReport, workspace: Path, round_no: int) -> bool:
        """用 codex 修一轮。

        ## 为什么默认 max_items=1 + single_file=True（实测校准）

        原配置 `max_items=20` + 420s 超时，在 smokeshow 上实测：
        codex 跑了 7 分 4 秒，**只改了一个文件里的一处**就被看门狗杀掉，
        报告显示「超时（420s）」。成本极高产出极低。

        根因不是模型慢，而是**任务包太大**：一次给 20 个问题 + 10 对重复，
        模型会先规划再动手，规划完时间就到了。对照实验
        （`experiments/diag_codex_action.py`）显示：给**一条**明确指令，
        17 秒就能改完文件。

        所以改为：每轮只处理**一个问题**，且限定在**单个文件**内。
        多轮循环由 `Guardian` 驱动（每轮重新评估），整体仍是收敛的。
        这是「小步快跑」对「大步慢跑」的替换。
        """
        prompt = build_fix_prompt(report, workspace, self.max_items,
                                  single_file=self.single_file)
        if not prompt.strip():
            self.last_error = "没有可交给 LLM 修的项（只有 low 级或无可修项）"
            return False

        self.calls += 1
        self.last_error = ""
        # 记录本轮目标文件，便于报告里说清「这次在修哪」
        self.last_target = _pick_target_file(report) if self.single_file else ""
        try:
            codex_base_url, proxy = self._ensure_proxy()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"起协议代理失败：{type(exc).__name__}: {exc}"
            return False

        spec = cb.CodexSpec(
            prompt=prompt,
            workspace=Path(workspace),
            model=self.model,
            provider_base_url=codex_base_url,
            context_window=262_144,
            timeout_s=self.timeout_s,
            # 必须 danger-full-access：workspace-write 会被模型理解为只读，
            # 导致「上游 200、turn 完成、但什么都没做」（实测）
            sandbox="danger-full-access",
            isolate_auth=True,
        )
        stderr_path = Path(tempfile.gettempdir()) / f"cforge-fix-{os.getpid()}.log"
        try:
            code, events = cb.run_codex(
                spec,
                env_extra={spec.provider_env_key: self.api_key},
                stderr_path=stderr_path,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            if proxy is not None:
                try:
                    proxy.stop()
                except Exception:  # noqa: BLE001 —— 停代理失败不能盖住主结论
                    pass

        texts = [e.text() for e in events if e.is_message and e.text()]
        self.last_summary = " | ".join(texts)[:500]

        timed_out = any(e.kind == "timeout" for e in events)
        if timed_out:
            self.last_error = f"超时（{self.timeout_s}s）"
            return False

        # 把 codex 报的错误带出来。
        #
        # 为什么必须做：实测 codex 退出码 1 时，`last_summary` 与 `last_error`
        # **双双为空**，报告里只剩「修复器放弃（问题需人工判断）」——
        # 读者会以为「模型觉得没法修」，而真实原因是上游 404
        # （codex 打了 `{base_url}/responses`，书生没有这个端点，必须走
        # compat_proxy）。这个误导会让人往完全错误的方向排查。
        #
        # 本项目自己的原则是「反馈的价值不在『有问题』，在『指向真病因』」，
        # 故这里把 error 事件的 message 收进 last_error。
        err_msgs = [
            str((e.payload.get("error") or {}).get("message")
                or (e.payload.get("item") or {}).get("message")
                or e.payload.get("message") or "")
            for e in events
            if e.kind in ("error", "turn.failed")
        ]
        err_msgs = [m for m in err_msgs if m]
        if code != 0 and err_msgs:
            # 去重并保留首末两条（首条常是根因，末条是最终失败态）
            uniq = list(dict.fromkeys(err_msgs))
            brief = " / ".join(uniq[:2])
            if len(uniq) > 2:
                brief += f" …（共 {len(uniq)} 条）"
            self.last_error = f"codex exit={code}：{brief}"

        if self.verbose:
            print(f"    [fix] exit={code} {self.last_summary[:120]}")
            if self.last_error:
                print(f"    [fix] error={self.last_error[:200]}")

        # 只要 codex 正常结束就算「尝试过」，是否真的改好由净改善门判定。
        # 这里返回 True 而不是「有没有变干净」，因为判定权必须留给门。
        return code == 0


@dataclass
class ScriptedFixAgent:
    """确定性修复器：只做 AST 能保证安全的修复，不调 LLM。

    用途有两个：
    1. 在无 API key 时也能演示完整守门链路
    2. 作为「便宜的第一层」：unused_import 这类问题用 AST 直接删，
       比让 LLM 处理快几百倍，也更可靠
    """

    workspace: Path | None = None
    calls: int = 0
    verbose: bool = False

    def fix(self, report: QualityReport, workspace: Path, round_no: int) -> bool:
        self.calls += 1
        changed = 0

        # 分级由 cforge.quality.repair 统一决定，避免「这里写死 unused_import、
        # 别处又写死一份清单」的两套标准。当前 auto 档只有 unused_import。
        from cforge.quality.repair import is_auto_fixable, is_blocking

        if any(is_blocking(s.kind) for s in report.smells):
            if self.verbose:
                print("    [scripted-fix] 存在阻断型问题（语法/读文件），跳过自动修复")
            return False

        by_file: dict[str, list] = {}
        for s in report.smells:
            if is_auto_fixable(s.kind) and s.severity in ("medium", "high"):
                by_file.setdefault(s.path, []).append(s)

        for rel, smells in by_file.items():
            path = Path(workspace) / rel
            if not path.is_file():
                continue
            if self._remove_imports(path, {s.line for s in smells}):
                changed += 1

        if self.verbose:
            print(f"    [scripted-fix] 处理 {changed} 个文件")
        return changed > 0

    @staticmethod
    def _remove_imports(path: Path, lines: set[int]) -> bool:
        """按行号删掉未使用的 import 语句。保持原有编码。"""
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
            return False  # 编码有问题就不碰，交给上层报错

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False

        src_lines = source.splitlines(keepends=True)
        kept = [
            ln for i, ln in enumerate(src_lines, 1)
            if i not in lines
        ]
        if len(kept) == len(src_lines):
            return False
        try:
            # 删完必须还能解析，否则放弃
            import ast
            ast.parse("".join(kept))
        except SyntaxError:
            return False
        path.write_text("".join(kept), encoding="utf-8", newline="")
        return True
