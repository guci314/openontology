"""智能体宿主适配层（PSM）——把本体里的卡片任务交给真实执行宿主。

本体层（``open_ontology/kanban.py``）只认业务事实：卡片、列、认领、裁决、证据。
本模块负责把「任务简报」交给某个真实宿主执行，并把结果**结构化**地交回本体。

两个宿主，都实现同一个 :class:`AgentHost` 协议：

* :class:`CodeBuddyHost` —— 命令行宿主。用 ``--output-format json``
  + ``--json-schema`` 让宿主**直接产出结构化裁决**（实测 ``structured_output``
  字段即为 schema 校验后的 JSON 对象），裁决不再从散文里 grep。
* :class:`OpenCodeHost` —— 本机 OpenCode 服务宿主，走真实 HTTP 端点
  （``/api/session`` → ``/move`` → ``/prompt`` → ``/wait`` → ``/message``）；
  也支持 ``opencode api`` 命令行传输，供已装桌面版的机器直接用。

两个宿主都返回同一种 :class:`HostResult`，本体因此不认宿主、不认凭据，
换宿主只是换一个实现类。

:func:`BoardRunner` 是本体与宿主之间的胶水：认领 → 执行 → 登记结果。

会话不再自动换代：状态驻留在 SQLite 本体库，人类手动开一个空会话后，
用 :func:`takeover_board_from_db` 按对象 ID（如 ``BD-CC-001``）定位实例、
还原 ``Board.goal`` / ``Board.state`` / 卡片分布并接管执行。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .aspects import IdleAwareCallbackAspect

__all__ = [
    "RESULT_SCHEMA",
    "HostTask",
    "HostResult",
    "AgentHost",
    "CallableHost",
    "CodeBuddyHost",
    "OpenCodeHost",
    "RoleRoutedHost",
    "BoardRunner",
    "extract_json_object",
    "parse_codebuddy_output",
    "parse_opencode_messages",
    "find_ontology_sqlite_files",
    "locate_object_in_databases",
    "takeover_board_from_db",
]


# ---------------------------------------------------------------------------
# 结构化输出契约：本体、宿主、裁判三者共用的同一形状
# ---------------------------------------------------------------------------
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "blocked", "failed"],
            "description": "completed=做完；blocked=需要人介入；failed=没做成",
        },
        "verdict": {
            "type": "string",
            "enum": ["PASS", "FAIL"],
            "description": "status=completed 时给出 PASS 或 FAIL",
        },
        "summary": {
            "type": "string",
            "description": "一句话说明做了什么、结论是什么",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["metric", "value"],
            },
            "description": "实际跑出来的数字，value 必须含数字",
        },
        "block_kind": {
            "type": "string",
            "enum": [
                "dependency",
                "awaiting_review",
                "external_dependency",
                "insufficient_input",
            ],
        },
        "artifact_uri": {"type": "string"},
        "board_state": {
            "type": ["string", "null"],
            "description": (
                "status=completed 且 verdict=PASS 时必须给出："
                "完工后新的看板基线状态，必须非空且与现有 Board.state 不同。"
                "failed/blocked 结果不需要该字段。"
            ),
        },
        "goal_changed": {
            "type": ["boolean", "null"],
            "description": (
                "status=completed 时必须显式评估目标是否变化："
                "true=目标已改，false=确认未改；null/省略表示未评估，会被本体拒绝。"
            ),
        },
        "goal_review_reason": {
            "type": "string",
            "description": (
                "status=completed 时必须给出目标评估理由，不能为空。"
                "failed/blocked 结果不需要该字段。"
            ),
        },
        "board_goal": {
            "type": ["string", "null"],
            "description": (
                "goal_changed=true 时必须给出：写入 Board.goal 的新目标全文，"
                "必须非空且与现有目标不同。goal_changed=false 时不要提交。"
            ),
        },
    },
    "required": ["status", "summary", "evidence"],
}

#: 给只能靠提示词约束的宿主用的输出契约说明。
_OUTPUT_CONTRACT = (
    "完成后最后一行只输出一个 JSON 对象，不要任何解释、Markdown 代码块或额外文字。\n"
    "字段：status(completed|blocked|failed)、verdict(PASS|FAIL)、"
    "summary(一句话)、evidence(数组，每项含 metric 与 value)、"
    "block_kind(仅 status=blocked 时给)、artifact_uri(可选)。\n"
    "status=completed 且 verdict=PASS 时，还必须给出："
    "board_state(完工后的新看板基线，非空且不同于现有 Board.state)、"
    "goal_changed(true/false，显式评估目标是否变化，不能省略)、"
    "goal_review_reason(非空的目标评估理由)；"
    "goal_changed=true 时还必须给出新的 board_goal。"
    "failed/blocked 结果不需要这些检查点字段。\n"
    "evidence 至少一条，且每条的 value 必须包含实际跑出来的数字"
    "（测试条数、覆盖格数、耗时等），不许复述转述稿。"
)


# ---------------------------------------------------------------------------
# 宿主协议
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HostTask:
    """交给宿主的一次任务。"""

    task_id: str
    briefing: str
    workspace_dir: str
    schema: dict[str, Any] = field(default_factory=lambda: dict(RESULT_SCHEMA))
    tools: tuple[str, ...] = ()
    max_turns: int | None = None
    timeout: float = 900.0
    model: str | None = None
    #: 续接既有会话（崩溃后重试时用同一个会话，不丢上下文）。
    resume_session_id: str | None = None
    #: 执行该任务的成员角色（architect / reviewer / developer / tester）。
    #: 仅用于宿主路由，**不进入业务对象**。
    role: str = ""


@dataclass(frozen=True)
class HostResult:
    """宿主回给本体的结构化结果。"""

    status: str  # completed | blocked | failed
    verdict: str | None = None  # PASS | FAIL
    summary: str = ""
    evidence: tuple[dict[str, Any], ...] = ()
    artifact_uri: str = ""
    session_id: str | None = None
    raw: str = ""
    error: str | None = None
    #: 完工检查点：新看板基线（未提供时为 None，交给本体拒绝）。
    board_state: str | None = None
    #: 目标评估：True=已改，False=显式确认未改，None=未评估。
    goal_changed: bool | None = None
    #: 目标评估理由；完工时必填。
    goal_review_reason: str = ""
    #: goal_changed=True 时的新目标全文。
    board_goal: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.verdict == "PASS"


@runtime_checkable
class AgentHost(Protocol):
    """智能体宿主统一协议。"""

    name: str

    def run(self, task: HostTask) -> HostResult: ...


@dataclass
class CallableHost:
    """把任意 Python 函数包装成宿主（测试与内嵌 Agent）。"""

    fn: Any
    name: str = "callable"

    def run(self, task: HostTask) -> HostResult:
        return self.fn(task)


# ---------------------------------------------------------------------------
# 解析：把宿主的原始输出变成 HostResult
# ---------------------------------------------------------------------------
def extract_json_object(text: str) -> dict[str, Any] | None:
    """从一段文本里抠出**最后一个**平衡的 JSON 对象。

    宿主常常在正文里解释两句再给 JSON，所以不能只取整段解析；
    但也不能取第一个——前面可能引用过别人的 JSON。
    """
    text = text or ""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    last: dict[str, Any] | None = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        start = -1
                        continue
                    if isinstance(parsed, dict):
                        last = parsed
    return last


def _result_from_payload(
    payload: dict[str, Any], raw: str, session_id: str | None = None
) -> HostResult:
    """把符合契约的 JSON 载荷变成 HostResult。"""
    status = str(payload.get("status") or "").strip()
    if status not in ("completed", "blocked", "failed"):
        return HostResult(
            status="failed",
            summary=str(payload.get("summary") or "")[:2000],
            raw=raw,
            session_id=session_id,
            error=f"INVALID_HOST_STATUS:{status!r}",
        )
    verdict = payload.get("verdict")
    if verdict is not None:
        verdict = str(verdict).strip().upper()
        if verdict not in ("PASS", "FAIL"):
            verdict = None
    evidence_raw = payload.get("evidence")
    evidence: list[dict[str, Any]] = []
    if isinstance(evidence_raw, list):
        for item in evidence_raw:
            if not isinstance(item, dict):
                continue
            metric = str(item.get("metric") or "").strip()
            value = str(item.get("value") or "").strip()
            if not metric or not value:
                continue
            evidence.append(
                {
                    "metric": metric,
                    "value": value,
                    "detail": str(item.get("detail") or ""),
                }
            )
    goal_changed_raw = payload.get("goal_changed")
    goal_changed = goal_changed_raw if isinstance(goal_changed_raw, bool) else None

    def _optional_text(key: str) -> str | None:
        value = payload.get(key)
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    review_raw = payload.get("goal_review_reason")
    goal_review_reason = review_raw.strip() if isinstance(review_raw, str) else ""

    return HostResult(
        status=status,
        verdict=verdict,
        summary=str(payload.get("summary") or "")[:4000],
        evidence=tuple(evidence),
        artifact_uri=str(payload.get("artifact_uri") or ""),
        session_id=session_id,
        raw=raw,
        board_state=_optional_text("board_state"),
        goal_changed=goal_changed,
        goal_review_reason=goal_review_reason,
        board_goal=_optional_text("board_goal"),
    )


def parse_codebuddy_output(raw: str) -> HostResult:
    """解析 ``codebuddy --output-format json`` 的输出。

    输出是一个消息数组，最后一条 ``type == "result"`` 的消息里带
    ``structured_output``（schema 校验后的 JSON）与 ``session_id``。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HostResult(
            status="failed",
            raw=raw[:4000],
            error=f"HOST_OUTPUT_NOT_JSON:{exc}",
        )
    entries = data if isinstance(data, list) else [data]
    result_entry = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "result":
            result_entry = entry
    if result_entry is None:
        return HostResult(
            status="failed",
            raw=raw[:4000],
            error="HOST_OUTPUT_WITHOUT_RESULT",
        )
    session_id = result_entry.get("session_id")
    structured = result_entry.get("structured_output")
    if isinstance(structured, dict):
        return _result_from_payload(structured, raw, session_id)
    # 没给 schema 时退回正文里抠 JSON
    text = str(result_entry.get("result") or "")
    if not text:
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            for part in entry.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text:
                        break
            if text:
                break
    payload = extract_json_object(text)
    if payload is None:
        return HostResult(
            status="failed",
            summary=text[:2000],
            raw=raw[:4000],
            session_id=session_id,
            error="HOST_OUTPUT_WITHOUT_JSON",
        )
    return _result_from_payload(payload, raw, session_id)


def parse_opencode_messages(raw: str) -> HostResult:
    """解析 ``GET /api/session/{id}/message`` 的返回。

    消息是**倒序**的（最新在前），且用户消息的 content 不是列表。
    因此从第一条带文本的助手消息里取结果。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HostResult(
            status="failed", raw=raw[:4000], error=f"HOST_OUTPUT_NOT_JSON:{exc}"
        )
    messages = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(messages, dict):
        messages = messages.get("messages", [])
    texts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("type") != "assistant":
            continue
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "")
                if text.strip():
                    texts.append(text)
    if not texts:
        return HostResult(
            status="failed", raw=raw[:4000], error="HOST_OUTPUT_WITHOUT_TEXT"
        )
    payload = extract_json_object(texts[0])
    if payload is None:
        return HostResult(
            status="failed",
            summary=texts[0][:2000],
            raw=raw[:4000],
            error="HOST_OUTPUT_WITHOUT_JSON",
        )
    return _result_from_payload(payload, raw)


# ---------------------------------------------------------------------------
# CodeBuddy 宿主（命令行）
# ---------------------------------------------------------------------------
@dataclass
class CodeBuddyHost:
    """命令行宿主：用 ``--json-schema`` 直接拿到结构化裁决。"""

    executable: str = "codebuddy"
    model: str = "deepseek-v4.1-flash"
    tools: tuple[str, ...] = ("Bash", "Edit", "Read", "Write", "Glob", "Grep")
    name: str = "codebuddy-cli"
    env_extra: dict[str, str] = field(default_factory=dict)

    def _binary(self) -> str:
        found = shutil.which(self.executable)
        if found:
            return found
        fallback = Path.home() / ".local" / "bin" / self.executable
        if fallback.exists():
            return str(fallback)
        raise FileNotFoundError(f"executable {self.executable!r} not found")

    def run(self, task: HostTask) -> HostResult:
        binary = self._binary()
        briefing = f"{task.briefing}\n\n{_OUTPUT_CONTRACT}"
        command = [
            binary,
            "-p",
            "-y",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(task.schema, ensure_ascii=False),
        ]
        if task.model or self.model:
            command += ["--model", task.model or self.model]
        if task.tools or self.tools:
            # ``--allowedTools`` 是变长参数：必须传逗号分隔的单个字符串。
            # 否则紧跟其后的位置参数（briefing）会被吞掉当成工具名。
            command += ["--allowedTools", ",".join(task.tools or self.tools)]
        if task.max_turns:
            command += ["--max-turns", str(task.max_turns)]
        if task.resume_session_id:
            command += ["--resume", task.resume_session_id, "--resume-create-missing"]
        command += ["--permission-mode", "bypassPermissions"]
        command.append(briefing)

        env = dict(os.environ, CODEBUDDY_IS_SANDBOX="1")
        env.update(self.env_extra)
        with tempfile.TemporaryDirectory(prefix="host-") as tmp:
            out_path = Path(tmp) / "out.json"
            try:
                with open(out_path, "wb") as out:
                    completed = subprocess.run(
                        command,
                        cwd=task.workspace_dir or None,
                        stdout=out,
                        stderr=subprocess.DEVNULL,
                        env=env,
                        timeout=task.timeout,
                        check=False,
                    )
            except subprocess.TimeoutExpired:
                return HostResult(
                    status="failed",
                    error=f"HOST_TIMEOUT:{task.timeout}s",
                )
            except OSError as exc:
                return HostResult(status="failed", error=f"HOST_UNAVAILABLE:{exc}")
            raw = out_path.read_text(encoding="utf-8", errors="replace")
            if completed.returncode != 0 and not raw.strip():
                return HostResult(
                    status="failed",
                    error=f"HOST_EXIT:{completed.returncode}",
                )
        return parse_codebuddy_output(raw)


# ---------------------------------------------------------------------------
# OpenCode 宿主（真实 HTTP 端点 / 命令行传输）
# ---------------------------------------------------------------------------
def _default_callback_text(payload: dict[str, Any]) -> str:
    """把看板回调载荷转成一段人可读的默认摘要文本。"""
    head = "【卡片回调】"
    card_id = str(payload.get("card_id") or "").strip()
    if card_id:
        head += card_id
    status = str(payload.get("status") or "").strip()
    verdict = str(payload.get("verdict") or "").strip()
    if status or verdict:
        head += " · " + " / ".join(part for part in (status, verdict) if part)
    lines = [head]
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"摘要：{summary}")
    evidence = payload.get("evidence")
    if isinstance(evidence, (list, tuple)) and evidence:
        lines.append("证据：")
        for item in evidence:
            if not isinstance(item, dict):
                continue
            metric = str(item.get("metric") or "").strip()
            value = str(item.get("value") or "").strip()
            if metric or value:
                lines.append(f"- {metric}: {value}")
    return "\n".join(lines)


@dataclass
class OpenCodeHost:
    """本机 OpenCode 服务宿主。

    ``base_url`` 给了就直接走 HTTP（需要 ``api_key``）；
    没给就用 ``opencode api`` 命令行传输，由桌面版自己处理端口与凭据。

    **世代轮转（Session Epoch Rotation）**：同一个 Session 长期使用会累积
    巨量 context，既慢又贵。设置 ``rotate_every_n_tasks`` 或
    ``max_session_tokens`` 后，宿主会在恰当的时候自动换一个干净的新会话，
    并在新世代的首轮任务前注入 ``handover_briefing``（同一世代内不重复注入）。
    """

    base_url: str | None = None
    api_key: str | None = None
    cli: str = "opencode"
    model: str | None = None
    name: str = "opencode"
    timeout: float = 900.0
    #: 每执行 N 个任务换一次新会话；``None`` 表示不按次数轮转。
    rotate_every_n_tasks: int | None = None
    #: 会话 token 上限；超过即立刻换代。``None`` 表示不按 token 轮转。
    max_session_tokens: int | None = None
    #: 新世代会话的标题前缀。
    session_title_prefix: str = "🏛️ Argon-Arch"
    #: 每个新世代首轮任务前注入的交接摘要（同世代内不重复注入）。
    handover_briefing: str = ""
    #: 动态交接包：换代时实时调用，取到非空文本即优先于静态摘要。
    handover_fn: Callable[[], str] | None = None

    # -- 运行态（不参与构造参数） -----------------------------------------
    active_session_id: str | None = field(default=None, init=False)
    active_session_tasks: int = field(default=0, init=False)
    session_history: list[str] = field(default_factory=list, init=False)

    # -- transport ---------------------------------------------------------
    def _http(self, method: str, path: str, payload: dict | None) -> str:
        url = self.base_url.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"unavailable: {exc}") from exc

    def _cli(self, method: str, path: str, payload: dict | None) -> str:
        command = [self.cli, "api", method.lower(), path]
        if payload is not None:
            command += ["--data", json.dumps(payload, ensure_ascii=False)]
        # 落盘再读：长会话的返回超过管道缓冲会被截断。
        with tempfile.TemporaryDirectory(prefix="host-") as tmp:
            out_path = Path(tmp) / "out.json"
            with open(out_path, "wb") as out:
                completed = subprocess.run(
                    command,
                    stdout=out,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout,
                    check=False,
                )
            raw = out_path.read_text(encoding="utf-8", errors="replace")
        if completed.returncode != 0 and not raw.strip():
            raise RuntimeError(f"opencode api exited {completed.returncode}")
        return raw

    def _request(self, method: str, path: str, payload: dict | None = None) -> str:
        if self.base_url:
            return self._http(method, path, payload)
        return self._cli(method, path, payload)

    @staticmethod
    def _unwrap(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"HOST_OUTPUT_NOT_JSON:{exc}") from exc
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, dict) else {}

    # -- lifecycle ---------------------------------------------------------
    def _create_session(self, title: str) -> str:
        payload: dict[str, Any] = {"title": title}
        model_ref = self.to_model_ref(self.model)
        if model_ref:
            payload["model"] = model_ref
        raw = self._request("post", "/api/session", payload)
        return str(self._unwrap(raw).get("id") or "")

    def _move(self, session_id: str, directory: str) -> None:
        try:
            self._request(
                "post", f"/api/session/{session_id}/move", {"directory": directory}
            )
        except RuntimeError:
            pass  # 目录已一致时服务端可能直接拒绝，忽略即可

    @staticmethod
    def to_model_ref(model: Any) -> dict[str, Any] | None:
        """把模型标识转成 OpenCode 要求的 ``Model.Ref`` 字典。

        OpenCode 的 ``/api/session`` 与 ``/prompt`` 只接受
        ``{"providerID": ..., "id": ...}`` 或 ``None``；传字符串会得到
        ``Expected Model.Ref | null at ["model"]`` 的 HTTP 400。
        """
        if isinstance(model, dict):
            if model.get("providerID") and model.get("id"):
                return dict(model)
            return None
        text = str(model or "").strip()
        if not text:
            return None
        if "/" in text:
            provider, _, model_id = text.partition("/")
            return {"providerID": provider, "id": model_id}
        return None

    def _prompt(self, session_id: str, text: str) -> None:
        payload: dict[str, Any] = {"text": text}
        model_ref = self.to_model_ref(self.model)
        if model_ref:
            payload["model"] = model_ref
        self._request("post", f"/api/session/{session_id}/prompt", payload)

    def _wait(self, session_id: str) -> None:
        self._request("post", f"/api/experimental/session/{session_id}/wait")

    def _messages(self, session_id: str) -> str:
        return self._request("get", f"/api/session/{session_id}/message")

    def _session_info(self, session_id: str) -> dict[str, Any]:
        """取单个会话的元数据（失败时返回空字典）。"""
        try:
            return self._unwrap(self._request("get", f"/api/session/{session_id}"))
        except (RuntimeError, OSError):
            return {}

    def _get_session_tokens(self, session_id: str) -> int:
        """会话累计 token（input + output + reasoning）。

        接口没返回 ``tokens`` 时安全回退 ``0``，绝不因此阻断执行。
        """
        if not session_id:
            return 0
        info = self._session_info(session_id)
        tokens = info.get("tokens") if isinstance(info, dict) else None
        if not isinstance(tokens, dict):
            return 0
        total = 0
        for key in ("input", "output", "reasoning"):
            try:
                total += int(tokens.get(key) or 0)
            except (TypeError, ValueError):
                continue
        return total

    # -- idle awareness ----------------------------------------------------
    def is_session_idle(self, session_id: str) -> bool:
        """会话是否处于空闲（``time.idle >= time.busy``）。

        接口没给 ``time`` 时按「零忙零闲」处理，视为空闲——宁可投递也不要
        把回调永远卡在队列里。
        """
        if not session_id:
            return True
        info = self._session_info(session_id)
        timing = info.get("time") if isinstance(info, dict) else None
        if not isinstance(timing, dict):
            timing = {}

        def _as_int(key: str) -> int:
            try:
                return int(timing.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return _as_int("idle") >= _as_int("busy")

    def wait_until_idle(
        self,
        session_id: str,
        quiet_seconds: float = 0.0,
        timeout: float = 120.0,
    ) -> bool:
        """等到会话空闲且持续安静 ``quiet_seconds`` 秒；超时返回 ``False``。"""
        if not session_id:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                self._wait(session_id)
            except Exception:
                pass  # 等待端点不可用不致命，交给下面的忙闲判定
            if self.is_session_idle(session_id):
                if quiet_seconds <= 0:
                    return True
                time.sleep(quiet_seconds)
                if self.is_session_idle(session_id):
                    return True
            if time.monotonic() >= deadline:
                return False

    def as_callback_aspect(
        self,
        formatter: Callable[[dict[str, Any]], str] | None = None,
    ) -> IdleAwareCallbackAspect:
        """造一个绑定到本宿主的忙闲感知回调切面。"""
        render = formatter or _default_callback_text

        def _send(target_id: str, payload: dict[str, Any]) -> None:
            self._prompt(target_id, render(payload))

        return IdleAwareCallbackAspect(
            is_idle_fn=self.is_session_idle,
            send_fn=_send,
            wait_idle_fn=self.wait_until_idle,
        )

    def _next_generation_title(self) -> str:
        return f"{self.session_title_prefix} #{len(self.session_history) + 1}"

    def _rotation_enabled(self) -> bool:
        """是否启用了世代轮转。

        两个阈值都没配置时**不启用轮转**，保持原有「每个任务新建会话」的
        行为（完全向后兼容既有测试与既有调用方）。
        """
        return bool(self.rotate_every_n_tasks or self.max_session_tokens)

    def _should_rotate(self) -> bool:
        """是否该丢弃当前会话、开启新世代。"""
        if not self.active_session_id:
            return True
        if self.rotate_every_n_tasks == 1:
            # 显式设为 1：每张卡都开新会话。
            return True
        if not self._rotation_enabled():
            # 未启用轮转：沿用原有行为，每个任务开新会话。
            return True
        if self.rotate_every_n_tasks and self.rotate_every_n_tasks > 1:
            if self.active_session_tasks >= self.rotate_every_n_tasks:
                return True
        if self.max_session_tokens and self.max_session_tokens > 0:
            if self._get_session_tokens(self.active_session_id) > self.max_session_tokens:
                return True
        return False

    def run(self, task: HostTask) -> HostResult:
        briefing = f"{task.briefing}\n\n{_OUTPUT_CONTRACT}"
        session_id = ""
        is_new_generation = False
        try:
            # ---- 决定复用还是换代 --------------------------------------
            if task.resume_session_id:
                # 显式续接：完全按调用方意图，不参与轮转。
                session_id = task.resume_session_id
            elif self._should_rotate():
                session_id = self._create_session(self._next_generation_title())
                if not session_id:
                    return HostResult(
                        status="failed", error="HOST_SESSION_CREATE_FAILED"
                    )
                self.active_session_id = session_id
                self.active_session_tasks = 0
                self.session_history.append(session_id)
                is_new_generation = True
            else:
                session_id = self.active_session_id or ""

            # 新世代的首轮任务前置注入交接摘要（同世代内不重复注入）。
            # 优先用动态交接包：本体里的看板基线此刻是什么，就交接什么。
            if is_new_generation:
                dynamic_handover = (
                    self.handover_fn() if self.handover_fn is not None else ""
                )
                handover_text = dynamic_handover or self.handover_briefing
                if handover_text:
                    briefing = (
                        f"【交接摘要 · 由上一代会话自动交接】\n"
                        f"{handover_text}\n"
                        f"【交接摘要结束】\n\n{briefing}"
                    )

            if task.workspace_dir:
                self._move(session_id, task.workspace_dir)
            self._prompt(session_id, briefing)
            self._wait(session_id)
            raw = self._messages(session_id)
        except subprocess.TimeoutExpired:
            return HostResult(
                status="failed",
                session_id=session_id or None,
                error=f"HOST_TIMEOUT:{task.timeout}s",
            )
        except (RuntimeError, OSError) as exc:
            return HostResult(
                status="failed",
                session_id=session_id or None,
                error=f"HOST_UNAVAILABLE:{exc}",
            )
        finally:
            if session_id and session_id == self.active_session_id:
                self.active_session_tasks += 1

        result = parse_opencode_messages(raw)
        if result.session_id is None:
            result = HostResult(
                status=result.status,
                verdict=result.verdict,
                summary=result.summary,
                evidence=result.evidence,
                artifact_uri=result.artifact_uri,
                session_id=session_id or None,
                raw=result.raw,
                error=result.error,
                board_state=result.board_state,
                goal_changed=result.goal_changed,
                goal_review_reason=result.goal_review_reason,
                board_goal=result.board_goal,
            )
        return result


# ---------------------------------------------------------------------------
# 按角色路由宿主
# ---------------------------------------------------------------------------
@dataclass
class RoleRoutedHost:
    """按成员角色把任务分发到不同宿主。

    本体只认 ``HostTask.role``，宿主怎么选是 PSM 的事：
    架构 / 评审走昂贵的推理模型，开发 / 测试走免费的执行模型。
    """

    routes: dict[str, AgentHost] = field(default_factory=dict)
    default_host: AgentHost | None = None
    name: str = "role-routed"

    def host_for(self, role: str) -> AgentHost | None:
        return self.routes.get((role or "").strip().lower()) or self.default_host

    def run(self, task: HostTask) -> HostResult:
        host = self.host_for(task.role)
        if host is None:
            return HostResult(
                status="failed",
                error=f"HOST_NOT_ROUTED:{task.role!r}",
            )
        return host.run(task)

    @classmethod
    def argon_and_codebuddy(
        cls,
        *,
        architect_roles: Sequence[str] = ("architect", "reviewer"),
        worker_roles: Sequence[str] = ("developer", "tester"),
        argon_model: str = "opencode-go/gemini-4-argon",
        worker_model: str = "deepseek-v4.1-flash",
        rotate_every_n_tasks: int | None = None,
        max_session_tokens: int | None = None,
        handover_briefing: str = "",
        handover_fn: Callable[[], str] | None = None,
        opencode_host: OpenCodeHost | None = None,
        codebuddy_host: CodeBuddyHost | None = None,
    ) -> "RoleRoutedHost":
        """默认组合：架构/评审走 Argon，开发/测试走 CodeBuddy。

        默认**不开启世代轮转**（``rotate_every_n_tasks`` / ``max_session_tokens``
        均为 ``None``）：主控会话何时开新窗由人类手动决定，不再自动换代。
        """
        argon = opencode_host or OpenCodeHost(
            model=argon_model,
            rotate_every_n_tasks=rotate_every_n_tasks,
            max_session_tokens=max_session_tokens,
            handover_briefing=handover_briefing,
            handover_fn=handover_fn,
        )
        worker = codebuddy_host or CodeBuddyHost(model=worker_model)
        routes: dict[str, AgentHost] = {}
        for role in architect_roles:
            routes[role] = argon
        for role in worker_roles:
            routes[role] = worker
        return cls(routes=routes, default_host=worker)


# ---------------------------------------------------------------------------
# 胶水：本体 ↔ 宿主
# ---------------------------------------------------------------------------
@dataclass
class BoardRunner:
    """把本体里的卡片交给宿主执行，并把结构化结果登记回本体。

    认领走本体动作（角色、忙闲、前置门控全部由本体判定）；
    执行走宿主；登记结果也走本体动作——所以「证据必须带数字」
    这条护栏在**最后一道关口**仍然生效：宿主给出没有数字的证据，
    本体会拒绝收单，这次执行不会被记成成功。
    """

    runtime: Any
    host: AgentHost
    actor: Any = None
    workspace_root: str = "."
    #: 可选回调切面（``IdleAwareCallbackAspect``）：收单后把摘要回投给发起方。
    callback_aspect: Any = None
    #: 回调目标会话 ID；为空表示不回投。
    notify_session_id: str = ""
    #: 投递前是否先等目标会话空闲（避免打断正在跑的会话）。
    wait_for_idle_callback: bool = False

    def _actor(self) -> Any:
        if self.actor is not None:
            return self.actor
        from .kanban import BOARD_MANAGER

        return BOARD_MANAGER

    def _workspace(self, board_key: str) -> str:
        root = Path(self.workspace_root).resolve()
        sub = root / board_key
        if sub.is_dir() or not (root / ".git").exists():
            sub.mkdir(parents=True, exist_ok=True)
            return str(sub)
        return str(root)

    @staticmethod
    def _read_doc(workspace: Path, relpath: str) -> str:
        """读取工作目录下的文档全文；文件不存在或内容为空时返回空串。"""
        path = workspace / relpath
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def sync_board_docs(
        self,
        board_key: str,
        *,
        goal_relpath: str = "docs/ARCHITECTURE.md",
        state_relpath: str = "_state.md",
    ) -> dict[str, str]:
        """把工作目录里的架构宪法与基线真值表**全文**写回 ``Board.goal`` / ``Board.state``。

        工作目录缺文件时回退到本体里已有的 ``goal`` / ``state``，
        因此这个方法是幂等的：有文档就覆盖成最新全文，没有就保持原样。
        """
        actor = self._actor()
        workspace = Path(self._workspace(board_key))
        goal_text = self._read_doc(workspace, goal_relpath)
        state_text = self._read_doc(workspace, state_relpath)

        board = self.runtime.get_object("Board", board_key, actor)
        if not state_text:
            state_text = str(getattr(board, "state", "") or "") or "基线已初始化"
        if not goal_text:
            goal_text = str(getattr(board, "goal", "") or "")

        self.runtime.submit_action(
            f"sync-docs-{board_key}-{uuid.uuid4().hex[:8]}",
            "update_board_state",
            {"board_key": board_key, "state": state_text, "goal": goal_text},
            actor,
        )
        return {"goal": goal_text, "state": state_text}

    def _member_role(self, member_id: str) -> str:
        """取成员角色，供宿主路由使用。

        角色只用于 PSM 选宿主，**不写回业务对象**；取不到就返回空串，
        由 ``RoleRoutedHost`` 回退到 ``default_host``。
        """
        try:
            member = self.runtime.get_object("Member", member_id, self._actor())
        except Exception:
            return ""
        return str(getattr(member, "role", "") or "")

    def make_handover_fn(self, board_key: str) -> Callable[[], str]:
        """造一个「现场取交接包」的闭包：每次换代都读本体里最新的看板状态。"""

        def _handover() -> str:
            from .kanban import render_board_handover

            overview = self.runtime.call_function(
                "board_overview", {"board_key": board_key}, self._actor()
            )
            return render_board_handover(overview)

        return _handover

    def fold_board_state(self, board_key: str) -> str:
        """把已完工卡片与工作目录真值文档折叠进 ``Board.state``，返回折叠后的全文。

        纯折叠：只写本体（``update_board_state`` 动作），**绝不创建或唤醒任何
        新 Session**。会话何时开启由人类手动决定，随后可用
        :func:`takeover_board_from_db` 从 SQLite 里按对象 ID 接管。
        """
        actor = self._actor()

        # 工作目录里的 _state.md / docs/ARCHITECTURE.md 是项目真值，存在且非空时优先采用。
        workspace = Path(self._workspace(board_key))
        workspace_state = self._read_doc(workspace, "_state.md")
        goal_override = self._read_doc(workspace, "docs/ARCHITECTURE.md")

        overview = self.runtime.call_function(
            "board_overview", {"board_key": board_key}, actor
        )
        prev_state = workspace_state or str(overview.get("state") or "").strip()
        done_items = [
            f"{card['card_id']}({card['title']}): {card.get('result') or 'done'}"
            for card in (overview.get("cards_summary") or [])
            if card.get("column_key") == "done"
            and (card.get("result") or card.get("title"))
        ]
        if done_items:
            latest = f"最新折叠完工：{'；'.join(done_items[-5:])}"
            kept = [
                line
                for line in prev_state.splitlines()
                if not line.strip().startswith("最新折叠完工：")
            ]
            folded_state = "\n".join([*kept, latest]).strip()
        else:
            folded_state = prev_state or "基线已初始化"

        fold_params: dict[str, Any] = {"board_key": board_key, "state": folded_state}
        if goal_override:
            fold_params["goal"] = goal_override
        self.runtime.submit_action(
            f"fold-{board_key}-{uuid.uuid4().hex[:8]}",
            "update_board_state",
            fold_params,
            actor,
        )
        return folded_state

    def _next_action_id(self, base: str) -> str:
        """取一个从未使用过的动作 ID，避免被缓存的 REJECTED/COMMITTED 记录短路。"""
        attempt = 1
        while True:
            candidate = base if attempt == 1 else f"{base}-R{attempt}"
            if self.runtime.get_action_run(candidate) is None:
                return candidate
            attempt += 1

    def _running_attempt(self, card_id: str, member_id: str) -> Any | None:
        """返回该成员在这张卡上仍然在途的执行尝试；没有则 None。"""
        from .kanban import CARD_RUNS, Card, CardRun

        actor = self._actor()
        try:
            card = self.runtime.get_object(Card, card_id, actor)
        except Exception:
            return None
        if card.assignee_id != member_id:
            return None
        runs = self.runtime.get_linked(card, CARD_RUNS, CardRun, actor)
        for run in reversed(runs):
            if run.status == "running" and run.member_id == member_id:
                return run
        return None

    def run_card(
        self,
        card_id: str,
        member_id: str,
        *,
        tools: Sequence[str] = (),
        max_turns: int | None = None,
        board_state: str | None = None,
        board_goal: str | None = None,
        goal_changed: bool | None = None,
        goal_review_reason: str | None = None,
    ) -> dict[str, Any]:
        """认领 → 执行 → 登记结果。返回执行摘要。"""
        from .kanban import card_briefing, render_card_briefing

        actor = self._actor()
        runtime = self.runtime
        briefing_data = runtime.call_function(
            "card_briefing", {"card_id": card_id}, actor
        )
        workspace = self._workspace(str(briefing_data.get("board_key") or "default"))
        member_role = self._member_role(member_id)

        column = briefing_data.get("column") or {}
        if column.get("purpose") == "completion":
            # 已完成卡片：不重复认领、不重复执行宿主、不重复计数。
            return {
                "card_id": card_id,
                "member_id": member_id,
                "status": "completed",
                "verdict": None,
                "summary": "卡片已在完成列，未重复执行。",
                "evidence": [],
                "outcome_recorded": "already_completed",
                "rejected_by_ontology": None,
                "session_id": None,
                "error": None,
                "callback_status": None,
            }

        # 只有当前成员确实持有在途尝试时才复用；否则申请新的认领。
        # 被拒的完工不会释放尝试，所以重试会落在同一 CardRun 上。
        if self._running_attempt(card_id, member_id) is None:
            runtime.submit_action(
                self._next_action_id(f"host-claim-{card_id}"),
                "claim_card",
                {"card_id": card_id, "member_id": member_id},
                actor,
            )

        task = HostTask(
            task_id=card_id,
            briefing=render_card_briefing(briefing_data),
            workspace_dir=workspace,
            tools=tuple(tools),
            max_turns=max_turns,
            role=member_role,
        )
        result = self.host.run(task)

        outcome = {
            "completed": "completed",
            "blocked": "blocked",
            "failed": "failed",
        }[result.status]
        if result.status == "completed" and result.verdict == "FAIL":
            outcome = "failed"

        params: dict[str, Any] = {
            "card_id": card_id,
            "member_id": member_id,
            "outcome": outcome,
            "summary": result.summary or result.error or "",
            "evidence": [dict(item) for item in result.evidence],
        }
        if result.artifact_uri:
            params["artifact_uri"] = result.artifact_uri
        if outcome == "blocked":
            params["block_kind"] = "awaiting_review"
        if outcome == "completed":
            # 显式调用方覆盖优先；否则采用宿主提议。目标决策与目标全文
            # 作为一个连贯评估：decision=False 时绝不携带宿主的矛盾目标。
            chosen_state = (
                board_state if board_state is not None else result.board_state
            )
            if chosen_state is not None:
                params["board_state"] = chosen_state
            chosen_decision = (
                goal_changed if goal_changed is not None else result.goal_changed
            )
            if chosen_decision is not None:
                params["goal_changed"] = chosen_decision
            if goal_review_reason is not None:
                # 显式空串/空白是调用方的无效输入，必须原样进入拒绝，
                # 不能回退到宿主理由。
                chosen_reason = goal_review_reason.strip()
            else:
                chosen_reason = (result.goal_review_reason or "").strip()
            if chosen_reason:
                params["goal_review_reason"] = chosen_reason
            chosen_goal = board_goal if board_goal is not None else result.board_goal
            if chosen_decision is True and chosen_goal is not None:
                params["board_goal"] = chosen_goal

        rejected: str | None = None
        try:
            runtime.submit_action(
                self._next_action_id(f"host-outcome-{card_id}"),
                "record_outcome",
                params,
                actor,
            )
        except Exception as exc:  # 护栏拒绝：证据不足 / 前置未齐 / 检查点缺失
            rejected = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"

        summary_dict: dict[str, Any] = {
            "card_id": card_id,
            "member_id": member_id,
            "status": result.status,
            "verdict": result.verdict,
            "summary": result.summary,
            "evidence": [dict(item) for item in result.evidence],
            "outcome_recorded": outcome if rejected is None else "rejected",
            "rejected_by_ontology": rejected,
            "session_id": result.session_id,
            "error": result.error,
        }

        # 回调投递：忙时暂存、闲时投递；切面自身保证绝不外抛异常，
        # 因此这里不会影响已经落盘的业务结果。
        cb_status: str | None = None
        if self.callback_aspect is not None and self.notify_session_id:
            cb_status = self.callback_aspect.deliver(
                self.notify_session_id,
                summary_dict,
                wait=self.wait_for_idle_callback,
            )
        summary_dict["callback_status"] = cb_status
        return summary_dict


# ---------------------------------------------------------------------------
# 空会话接管：按对象 ID 在 SQLite 本体库里定位实例
# ---------------------------------------------------------------------------
#: OpenOntology 实例库的文件后缀。
_SQLITE_SUFFIXES = frozenset({".sqlite3", ".db", ".sqlite"})
#: 扫描时跳过的目录。
_SQLITE_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """以只读方式打开 SQLite 文件；打不开（损坏 / 非库）时返回 ``None``。

    用 :meth:`Path.as_uri` 生成标准 ``file://`` URI：路径里的 ``#`` / ``?`` /
    ``%`` / 空格若直接拼进 ``file:{path}?mode=ro``，会被当作片段 / 查询串 /
    百分号转义，导致只读扫描打开（甚至创建）一个被截断的旁路文件。
    """
    try:
        conn = sqlite3.connect(
            Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
        )
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _has_ontology_objects(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_objects'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def find_ontology_sqlite_files(search_root: str | Path = ".") -> list[Path]:
    """递归扫描目录下所有合法的 OpenOntology SQLite 库。

    只看 ``.sqlite3`` / ``.db`` / ``.sqlite`` 文件，且必须含有
    ``ontology_objects`` 表——其它 SQLite 文件（缓存、第三方库）一律排除。
    """
    root = Path(search_root)
    if root.is_file():
        candidates = [root] if root.suffix.lower() in _SQLITE_SUFFIXES else []
    elif root.is_dir():
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in _SQLITE_SUFFIXES
            and not _SQLITE_SKIP_DIRS.intersection(path.parts)
        ]
    else:
        return []

    found: list[Path] = []
    for path in sorted(candidates):
        conn = _open_readonly(path)
        if conn is None:
            continue
        try:
            if _has_ontology_objects(conn):
                found.append(path)
        finally:
            conn.close()
    return found


def _row_matches_target(properties: Any, target: str) -> bool:
    """``properties`` 里是否引用了 ``target``（如卡片的 ``board_key``）。"""
    if not isinstance(properties, dict):
        return False
    return any(
        isinstance(value, str) and value == target for value in properties.values()
    )


def locate_object_in_databases(
    target_id: str, search_root: str | Path = "."
) -> list[dict[str, Any]]:
    """在所有 OpenOntology 库里查找 ``target_id`` 对应的本体对象。

    先按主键 ``object_id`` 精确命中；没有主键命中时，再在
    ``properties_json`` 里找引用了该 ID 的对象（如某张 ``Card`` 的
    ``board_key`` 指向目标看板）。
    """
    target = str(target_id or "").strip()
    if not target:
        return []

    matches: list[dict[str, Any]] = []
    for db_path in find_ontology_sqlite_files(search_root):
        conn = _open_readonly(db_path)
        if conn is None:
            continue
        try:
            rows = conn.execute(
                """
                SELECT object_type, object_id, title, properties_json
                FROM ontology_objects
                WHERE object_id = ?
                ORDER BY object_type, object_id
                """,
                (target,),
            ).fetchall()
            if not rows:
                rows = [
                    row
                    for row in conn.execute(
                        """
                        SELECT object_type, object_id, title, properties_json
                        FROM ontology_objects
                        ORDER BY object_type, object_id
                        """
                    ).fetchall()
                    if _row_matches_target(json.loads(row["properties_json"]), target)
                ]
            if not rows:
                continue
            registered_types = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM ontology_registry "
                    "WHERE kind='object_type' ORDER BY name"
                ).fetchall()
            ]
            for row in rows:
                matches.append(
                    {
                        "db_path": str(db_path.resolve()),
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                        "title": row["title"],
                        "properties": json.loads(row["properties_json"]),
                        "registered_types": list(registered_types),
                    }
                )
        except (sqlite3.Error, json.JSONDecodeError):
            continue
        finally:
            conn.close()
    return matches


def takeover_board_from_db(
    target_id: str, search_root: str | Path = "."
) -> dict[str, Any]:
    """在空会话里按对象 ID 接管一块看板。

    支持直接传 ``board_key``（如 ``"BD-CC-001"``），也支持传某张卡片的
    ``card_id`` 反查它所属的看板。命中后以该库打开 ``OntologyRuntime``，
    用 ``board_overview`` + ``render_board_handover`` 组装出完整上下文
    （``Board.goal`` + ``Board.state`` + 卡片分布），直接可驱动后续执行。

    查找不是初始化：查不到就抛 ``LookupError``，**绝不**顺手建目录、建库
    或播种演示数据。需要新建实例库请显式调用演示装配函数。
    """
    root = Path(search_root)
    matches = locate_object_in_databases(target_id, root)
    if not matches:
        raise LookupError(
            f"未在任何 OpenOntology 数据库中找到实例或对象 {target_id!r}"
        )

    # 优先取看板本体自身的命中，其次才按 properties 里的 board_key 反查。
    match = sorted(
        matches, key=lambda item: 0 if item["object_type"] == "Board" else 1
    )[0]
    if match["object_type"] == "Board":
        board_key = str(match["object_id"])
    else:
        board_key = str(match["properties"].get("board_key") or "")
    if not board_key:
        raise LookupError(
            f"命中对象 {match['object_type']}({match['object_id']}) 未关联任何看板"
        )

    from .kanban import BOARD_MANAGER, render_board_handover, spec as board_spec
    from .runtime import OntologyRuntime

    runtime = OntologyRuntime(board_spec, db_path=match["db_path"])
    overview = runtime.call_function(
        "board_overview", {"board_key": board_key}, BOARD_MANAGER
    )
    context = render_board_handover(overview)
    return {
        "db_path": match["db_path"],
        "board_key": board_key,
        "runtime": runtime,
        "overview": overview,
        "context": context,
        "match": match,
    }
