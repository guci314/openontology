"""外围可插拔机械臂驱动层（PSM 适配层）。

本体层（``dev_factory``）只认「工单 / 成员 / 交付物」这些业务概念；
真正把任务简报送到某个执行宿主上的细节，全部收敛在这里：

* :class:`HarnessDriver` —— 统一协议 ``execute_briefing(...) -> str``；
* :class:`CallableHarnessDriver` —— 任意 Python 函数，便于测试与内嵌；
* :class:`CodeBuddyCLIDriver` —— 命令行宿主；
* :class:`OpenCodeHTTPDriver` —— 会话式远端宿主；
* :class:`HermesCLIDriver` / :class:`GenericCLIDriver` —— 模板化命令行宿主；
* :class:`FactoryOrchestrator` —— 拉取工单、渲染简报、执行、回填交卷；
  当执行侧抛出 :class:`HarnessRateLimitError` / :class:`HarnessUnavailableError`
  时，自动调用本体的 ``reassign_ticket`` 做无损换人，然后继续接力。

业务账本（工单状态、返工轮次、交卷摘要、评审意见）始终保存在 Ontology 中，
因此换人 / 换宿主不会丢失任何上下文。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .dev_factory import FACTORY_MANAGER, render_briefing
from .runtime import OntologyRuntime
from .spec import Actor

__all__ = [
    "HarnessError",
    "HarnessRateLimitError",
    "HarnessUnavailableError",
    "HarnessDriver",
    "CallableHarnessDriver",
    "CodeBuddyCLIDriver",
    "OpenCodeHTTPDriver",
    "GenericCLIDriver",
    "HermesCLIDriver",
    "FactoryOrchestrator",
]


# ---------------------------------------------------------------------------
# 错误分类：驱动层只需把「限流」和「不可用」区分开
# ---------------------------------------------------------------------------
class HarnessError(RuntimeError):
    """机械臂执行失败的基类。"""


class HarnessRateLimitError(HarnessError):
    """执行宿主限流 / 配额耗尽（可换人继续）。"""


class HarnessUnavailableError(HarnessError):
    """执行宿主离线、超时或进程异常（可换人继续）。"""


_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
    "overloaded",
)


def _looks_rate_limited(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _run_cli(
    command: Sequence[str],
    workspace_dir: str,
    *,
    timeout: float,
    env: Mapping[str, str] | None,
    label: str,
) -> str:
    """执行一条命令行机械臂指令，并把失败映射成两类可恢复错误。"""
    try:
        completed = subprocess.run(
            list(command),
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else dict(env),
        )
    except FileNotFoundError as exc:
        raise HarnessUnavailableError(
            f"{label}: executable {command[0]!r} not found"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessUnavailableError(
            f"{label}: timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise HarnessUnavailableError(f"{label}: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if _looks_rate_limited(detail):
            raise HarnessRateLimitError(f"{label}: {detail[:400]}")
        raise HarnessUnavailableError(
            f"{label}: exit {completed.returncode}: {detail[:400]}"
        )
    return (completed.stdout or "").strip()


# ---------------------------------------------------------------------------
# 协议与内置驱动
# ---------------------------------------------------------------------------
@runtime_checkable
class HarnessDriver(Protocol):
    """机械臂统一协议。"""

    name: str

    def execute_briefing(
        self, engineer_id: str, briefing: str, workspace_dir: str
    ) -> str:
        """在 ``workspace_dir`` 内执行简报，返回执行结果文本。"""


@dataclass
class CallableHarnessDriver:
    """把任意 Python 函数包装成机械臂（测试 / 内嵌 Agent / 自定义宿主）。"""

    fn: Callable[[str, str, str], str]
    name: str = "callable"

    def execute_briefing(
        self, engineer_id: str, briefing: str, workspace_dir: str
    ) -> str:
        return str(self.fn(engineer_id, briefing, workspace_dir))


@dataclass
class CodeBuddyCLIDriver:
    """封装 ``codebuddy -p <briefing> -y`` 命令行调用。"""

    executable: str = "codebuddy"
    extra_args: Sequence[str] = ()
    timeout: float = 900.0
    name: str = "codebuddy-cli"
    env: Mapping[str, str] | None = None

    def execute_briefing(
        self, engineer_id: str, briefing: str, workspace_dir: str
    ) -> str:
        command = [self.executable, "-p", briefing, "-y", *self.extra_args]
        return _run_cli(
            command,
            workspace_dir,
            timeout=self.timeout,
            env=self.env,
            label=f"{self.name}[{engineer_id}]",
        )


@dataclass
class OpenCodeHTTPDriver:
    """封装会话式远端执行宿主的 JSON 调用。"""

    base_url: str
    session_path: str = "/session"
    timeout: float = 180.0
    name: str = "opencode-http"

    def execute_briefing(
        self, engineer_id: str, briefing: str, workspace_dir: str
    ) -> str:
        url = self.base_url.rstrip("/") + self.session_path
        payload = {
            "engineer_id": engineer_id,
            "briefing": briefing,
            "workspace_dir": workspace_dir,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code == 429 or _looks_rate_limited(detail):
                raise HarnessRateLimitError(
                    f"{self.name}[{engineer_id}]: HTTP {exc.code} {detail[:300]}"
                ) from exc
            raise HarnessUnavailableError(
                f"{self.name}[{engineer_id}]: HTTP {exc.code} {detail[:300]}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HarnessUnavailableError(
                f"{self.name}[{engineer_id}]: {exc}"
            ) from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body.strip()
        if isinstance(parsed, dict):
            for key in ("output", "result", "message", "content"):
                if parsed.get(key):
                    return str(parsed[key])
            return json.dumps(parsed, ensure_ascii=False)
        return body.strip()


@dataclass
class GenericCLIDriver:
    """模板化命令行驱动，可接入任意 CLI Agent。

    模板占位符：``{engineer_id}`` / ``{briefing}`` / ``{briefing_file}`` /
    ``{workspace_dir}``。简报会先落到工作目录内的临时文件，避免超长命令行。
    """

    command_template: Sequence[str]
    name: str = "generic-cli"
    timeout: float = 900.0
    env: Mapping[str, str] | None = None

    def execute_briefing(
        self, engineer_id: str, briefing: str, workspace_dir: str
    ) -> str:
        workspace = Path(workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="harness-", dir=str(workspace)) as tmp:
            briefing_file = Path(tmp) / "briefing.txt"
            briefing_file.write_text(briefing, encoding="utf-8")
            mapping = {
                "engineer_id": engineer_id,
                "briefing": briefing,
                "briefing_file": str(briefing_file),
                "workspace_dir": str(workspace),
            }
            command = [part.format(**mapping) for part in self.command_template]
            return _run_cli(
                command,
                str(workspace),
                timeout=self.timeout,
                env=self.env,
                label=f"{self.name}[{engineer_id}]",
            )


@dataclass
class HermesCLIDriver(GenericCLIDriver):
    """Hermes Agent 的命令行接入。"""

    command_template: Sequence[str] = (
        "hermes",
        "run",
        "--workspace",
        "{workspace_dir}",
        "--briefing-file",
        "{briefing_file}",
    )
    name: str = "hermes-cli"


# ---------------------------------------------------------------------------
# 编排器：工单 -> 简报 -> 机械臂 -> 回填交卷，失败自动换人
# ---------------------------------------------------------------------------
class FactoryOrchestrator:
    """把研发工厂本体接到任意机械臂上，并在故障时无损换人。"""

    def __init__(
        self,
        runtime: OntologyRuntime,
        *,
        actor: Actor | None = None,
        workspace_root: str | Path = ".",
        drivers: Mapping[str, HarnessDriver] | None = None,
    ) -> None:
        self.runtime = runtime
        self.actor = actor or FACTORY_MANAGER
        self.workspace_root = Path(workspace_root)
        self.drivers: dict[str, HarnessDriver] = {}
        self._bindings: dict[str, str] = {}
        for name, driver in (drivers or {}).items():
            self.drivers[name] = driver

    # -- registry ----------------------------------------------------------
    def register(self, driver: HarnessDriver, name: str | None = None) -> str:
        key = name or getattr(driver, "name", None) or f"driver-{len(self.drivers) + 1}"
        self.drivers[key] = driver
        return key

    def bind(self, engineer_id: str, driver_name: str) -> None:
        if driver_name not in self.drivers:
            raise KeyError(f"unknown driver {driver_name!r}")
        self._bindings[engineer_id] = driver_name

    def driver_for(self, engineer_id: str) -> HarnessDriver:
        driver_name = self._bindings.get(engineer_id)
        if driver_name is None:
            if len(self.drivers) == 1:
                return next(iter(self.drivers.values()))
            raise KeyError(f"engineer {engineer_id!r} is not bound to any driver")
        return self.drivers[driver_name]

    # -- helpers -----------------------------------------------------------
    def _briefing(self, ticket_id: str) -> dict[str, Any]:
        return self.runtime.call_function(
            "build_engineer_briefing", {"ticket_id": ticket_id}, self.actor
        )

    def _workspace(self, briefing: dict[str, Any]) -> str:
        scope = briefing.get("normalized_scope") or "."
        workspace = (self.workspace_root / scope).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace)

    def _submit(self, action: str, params: dict[str, Any], prefix: str) -> dict[str, Any]:
        run_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        return self.runtime.submit_action(run_id, action, params, self.actor)

    def _next_engineer(
        self, exclude: set[str], preferred: str | None = None
    ) -> Any:
        if preferred and preferred not in exclude:
            candidate = self.runtime.get_object("Engineer", preferred, self.actor)
            if candidate.role == "coder" and candidate.availability == "IDLE":
                return candidate
        engineers = self.runtime.query_objects("Engineer", self.actor)
        for engineer in sorted(engineers, key=lambda row: row.engineer_id):
            if engineer.engineer_id in exclude:
                continue
            if engineer.role == "coder" and engineer.availability == "IDLE":
                return engineer
        return None

    # -- main entry --------------------------------------------------------
    def step_ticket(
        self,
        ticket_id: str,
        *,
        fallback_engineer_id: str | None = None,
        submit: bool = True,
        artifact_uri: str = "",
        summary_limit: int = 2000,
    ) -> dict[str, Any]:
        """执行一张在途工单，必要时自动换人接力。

        返回 ``{"ticket_id", "engineer_id", "driver", "output", "failovers"}``。
        """
        ticket = self.runtime.get_object("WorkTicket", ticket_id, self.actor)
        if ticket.stage != "CODING":
            raise HarnessError(
                f"ticket {ticket_id} is {ticket.stage}, not CODING"
            )
        if not ticket.assignee_id:
            raise HarnessError(f"ticket {ticket_id} has no assignee")

        engineer_id: str | None = ticket.assignee_id
        tried: set[str] = set()
        failovers: list[dict[str, Any]] = []
        preferred = fallback_engineer_id

        while engineer_id is not None and engineer_id not in tried:
            tried.add(engineer_id)
            driver = self.driver_for(engineer_id)
            briefing = self._briefing(ticket_id)
            text = render_briefing(briefing)
            workspace = self._workspace(briefing)
            try:
                output = driver.execute_briefing(engineer_id, text, workspace)
            except (HarnessRateLimitError, HarnessUnavailableError) as exc:
                failovers.append(
                    {
                        "engineer_id": engineer_id,
                        "driver": getattr(driver, "name", type(driver).__name__),
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                replacement = self._next_engineer(tried, preferred=preferred)
                preferred = None
                if replacement is None:
                    raise HarnessUnavailableError(
                        f"ticket {ticket_id}: no idle coder left after "
                        f"{len(failovers)} failover(s)"
                    ) from exc
                self._submit(
                    "reassign_ticket",
                    {
                        "ticket_id": ticket_id,
                        "new_engineer_id": replacement.engineer_id,
                        "reason": str(exc)[:500],
                    },
                    prefix=f"orch-reassign-{ticket_id}",
                )
                engineer_id = replacement.engineer_id
                continue

            if submit:
                summary = (output or "").strip()[:summary_limit] or "执行完成"
                self._submit(
                    "submit_work",
                    {
                        "ticket_id": ticket_id,
                        "engineer_id": engineer_id,
                        "summary": summary,
                        "artifact_uri": artifact_uri,
                    },
                    prefix=f"orch-submit-{ticket_id}",
                )
            return {
                "ticket_id": ticket_id,
                "engineer_id": engineer_id,
                "driver": getattr(driver, "name", type(driver).__name__),
                "output": output,
                "failovers": failovers,
            }

        raise HarnessUnavailableError(
            f"ticket {ticket_id}: exhausted every bound engineer"
        )
