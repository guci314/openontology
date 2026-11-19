"""拟人化溯因编排器（OntologyBuilderAgent / OntologyEvolverAgent）与可插拔 LLM 后端（纯 M2）。

修复与保障：
* 系统提示词注入精确 JSON Schema（``DeclarativeBlueprint`` / ``AdapterMappingSpec`` / ``EvolutionPatch``
  与安全谓词 AST 操作符表），适配真实 LLM 调用；
* ``propose_json`` / ``model_validate`` / ``apply_evolution_patch`` 全程受 ``try/except`` 保护，
  格式或 Schema 偏差自动转成关 1 反例报告回喂给下一轮 CEGIS 自我修正，不再中途崩溃；
* 编排器物理上不接触 ``CRUCIBLE_ACTOR``，完全委托 ``SymbolicCrucible.verify_and_record()`` 独立签发报告。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from ..spec import Actor, InvariantError
from .crucible import CrucibleContext, CrucibleReport, SymbolicCrucible
from .dsl import (
    DeclarativeBlueprint,
    EvolutionPatch,
    ProposedEvalCase,
    apply_adapter_mapping_patch,
    apply_evolution_patch,
    dsl_schema_prompt_guide,
)
from .friction import FrictionClusterSummary
from .governance import BUILDER_ACTOR, EVOLVER_ACTOR, InductionGovernanceService
from .phenomenon import (
    AdapterMappingSpec,
    ExternalPhenomenonProfile,
    project_external_phenomenon,
)
from .seed import SeedOntology
from .utility import L2EvolutionTicket

__all__ = [
    "BUILDER_SYSTEM_PROMPT",
    "EVOLVER_SYSTEM_PROMPT",
    "L2_ARCHITECT_SYSTEM_PROMPT",
    "build_builder_system_prompt",
    "build_evolver_system_prompt",
    "build_l2_architect_system_prompt",
    "AbductionBackend",
    "CallableAbductionBackend",
    "GeminiAbductionBackend",
    "OpenAICompatibleAbductionBackend",
    "OpenRouterAbductionBackend",
    "AntigravityCLIAbductionBackend",
    "InductionRoundTrace",
    "InductionCycleResult",
    "OntologyBuilderAgent",
    "OntologyEvolverAgent",
    "OntologyL2ArchitectAgent",
]


class AbductionBackend(Protocol):
    def propose_json(self, *, system_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
        ...


class CallableAbductionBackend:
    """Deterministic or custom Python callback backend for tests and offline demos."""

    def __init__(self, fn: Callable[[str, str, int], dict[str, Any]]) -> None:
        self._fn = fn

    def propose_json(self, *, system_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
        return self._fn(system_prompt, user_prompt, round_index)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"LLM output did not contain a JSON object: {raw_text[:200]!r}")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return json.loads(text[start : end + 1])


def _read_http_body_with_deadline(
    req: urllib.request.Request,
    *,
    timeout_sec: float,
) -> bytes:
    """Open HTTP request and read full body under a hard wall-clock deadline."""
    total_budget = max(0.5, float(timeout_sec))
    deadline = time.monotonic() + total_budget
    chunks: list[bytes] = []
    with urllib.request.urlopen(req, timeout=min(30.0, total_budget)) as resp:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"HTTP request exceeded hard wall-clock deadline of {timeout_sec}s"
                )
            try:
                sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                if sock is not None and hasattr(sock, "settimeout"):
                    sock.settimeout(max(0.1, min(30.0, remaining)))
            except Exception:
                pass
            chunk = resp.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


class GeminiAbductionBackend:
    """Zero-dependency stdlib HTTP backend for Google Gemini JSON abduction."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        timeout_sec: int = 90,
    ) -> None:
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiAbductionBackend")
        self.model = model
        self.timeout_sec = timeout_sec

    def propose_json(self, *, system_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw_body = _read_http_body_with_deadline(req, timeout_sec=self.timeout_sec)
        body = json.loads(raw_body.decode("utf-8"))
        parts = body["candidates"][0]["content"].get("parts") or []
        non_thought = [p["text"] for p in parts if isinstance(p, dict) and not p.get("thought") and "text" in p]
        text = "\n".join(non_thought) if non_thought else str(parts[0]["text"])
        return _extract_json_object(text)


class OpenAICompatibleAbductionBackend:
    """Zero-dependency stdlib HTTP backend for OpenAI-compatible endpoints."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4o-mini",
        timeout_sec: int = 90,
        use_json_mode: bool = True,
    ) -> None:
        self.api_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAICompatibleAbductionBackend")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.use_json_mode = use_json_mode

    def propose_json(self, *, system_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "open-ontology/1.0",
            },
            method="POST",
        )
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                raw_body = _read_http_body_with_deadline(req, timeout_sec=self.timeout_sec)
                body = json.loads(raw_body.decode("utf-8"))
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        else:  # pragma: no cover
            assert last_err is not None
            raise last_err
        text = body["choices"][0]["message"]["content"]
        return _extract_json_object(text)


class OpenRouterAbductionBackend(OpenAICompatibleAbductionBackend):
    """Zero-dependency stdlib HTTP backend for OpenRouter (e.g. ``google/gemini-4-neon``)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "google/gemini-4-neon",
        timeout_sec: int = 120,
    ) -> None:
        resolved_key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
        if not resolved_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouterAbductionBackend")
        super().__init__(
            api_key=resolved_key,
            base_url=base_url or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            model=model,
            timeout_sec=timeout_sec,
            use_json_mode=True,
        )


class AntigravityCLIAbductionBackend:
    """Zero-dependency subprocess backend routing abduction through the signed-in ``agy`` CLI.

    Security & scientific integrity invariants:
    * Runs in a fresh empty temporary directory (``isolate_cwd=True``) with
      ``--disable-slash-commands`` so the CLI agent cannot read repository files
      or smuggle answers during zero-hint induction benchmarks.
    * Enforces ``num_turns == 1`` (``enforce_single_turn=True``) so tool-calling
      turns are rejected rather than silently accepted.
    * Resets conversation state whenever ``round_index <= 1`` and optionally
      reuses ``--conversation <id>`` on ``round_index > 1`` within a single CEGIS
      cycle so symbolic-gradient repair rounds benefit from context continuity.
    """

    def __init__(
        self,
        *,
        model: str = "gemini-4-neon-medium",
        cli_path: str = "agy",
        timeout_sec: int = 240,
        reuse_conversation: bool = True,
        isolate_cwd: bool = True,
        enforce_single_turn: bool = True,
        max_retries: int = 3,
        json_schema: dict[str, Any] | str | None = None,
    ) -> None:
        resolved_cli = shutil.which(cli_path) if not os.path.isabs(cli_path) else cli_path
        if not resolved_cli or not os.path.exists(resolved_cli):
            raise ValueError(f"Antigravity CLI executable not found: {cli_path!r}")
        self.cli_path = resolved_cli
        self.model = model
        self.timeout_sec = timeout_sec
        self.reuse_conversation = reuse_conversation
        self.isolate_cwd = isolate_cwd
        self.enforce_single_turn = enforce_single_turn
        self.max_retries = max(1, max_retries)
        self.json_schema = json_schema
        self._conversation_id: str | None = None
        self.last_conversation_id: str | None = None
        self.last_usage: dict[str, Any] | None = None
        self.last_duration_sec: float | None = None

    def _build_prompt(self, *, system_prompt: str, user_prompt: str, reusing: bool, round_index: int) -> str:
        guard = (
            "STRICT_ABDUCTION_MODE: Do NOT call any tools, do NOT inspect any files, "
            "and do NOT run commands. Output ONLY one raw valid JSON object and nothing else."
        )
        if reusing:
            return f"{guard}\n\n[CEGIS_ROUND_{round_index}_INPUT]\n{user_prompt}"
        return f"{guard}\n\n[SYSTEM_INSTRUCTIONS]\n{system_prompt}\n\n[USER_INPUT]\n{user_prompt}"

    def _run_once(
        self,
        *,
        prompt: str,
        conversation_id: str | None,
        cwd: str,
    ) -> dict[str, Any]:
        cmd = [
            self.cli_path,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--print-timeout",
            f"{self.timeout_sec}s",
        ]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.json_schema is not None:
            schema_arg = (
                self.json_schema
                if isinstance(self.json_schema, str)
                else json.dumps(self.json_schema, ensure_ascii=False, separators=(",", ":"))
            )
            cmd.extend(["--json-schema", schema_arg])
        cmd.extend(["--print", prompt])

        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=self.timeout_sec + 30,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                f"agy CLI failed (rc={proc.returncode}): {stderr or stdout[:300]}"
            )
        envelope = _extract_json_object(stdout)
        status = str(envelope.get("status", "")).upper()
        if status and status != "SUCCESS":
            raise RuntimeError(
                f"agy CLI returned non-SUCCESS status={status!r}: {stderr or stdout[:300]}"
            )
        return envelope

    def propose_json(self, *, system_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
        if round_index <= 1 or not self.reuse_conversation:
            self._conversation_id = None

        reusing = bool(self.reuse_conversation and round_index > 1 and self._conversation_id)
        prompt = self._build_prompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reusing=reusing,
            round_index=round_index,
        )

        last_err: Exception | None = None
        envelope: dict[str, Any] | None = None
        for attempt in range(self.max_retries):
            try:
                if self.isolate_cwd:
                    with tempfile.TemporaryDirectory(prefix="open_ontology_agy_") as tmp_dir:
                        envelope = self._run_once(
                            prompt=prompt,
                            conversation_id=self._conversation_id if reusing else None,
                            cwd=tmp_dir,
                        )
                else:
                    envelope = self._run_once(
                        prompt=prompt,
                        conversation_id=self._conversation_id if reusing else None,
                        cwd=os.getcwd(),
                    )
                break
            except Exception as exc:
                last_err = exc
                err_text = str(exc).lower()
                # If resuming a conversation failed, fall back to a clean full prompt
                fell_back_from_reuse = False
                if reusing and ("conversation" in err_text or "not found" in err_text):
                    reusing = False
                    fell_back_from_reuse = True
                    self._conversation_id = None
                    prompt = self._build_prompt(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        reusing=False,
                        round_index=round_index,
                    )
                is_transient = any(
                    tok in err_text
                    for tok in (
                        "eof",
                        "timeout",
                        "timed out",
                        "connection reset",
                        "temporarily unavailable",
                        "502",
                        "503",
                        "504",
                    )
                )
                if attempt == self.max_retries - 1 or (not is_transient and not fell_back_from_reuse):
                    raise
                time.sleep(1.0 * (attempt + 1))

        if envelope is None:  # pragma: no cover
            assert last_err is not None
            raise last_err

        num_turns = envelope.get("num_turns")
        if self.enforce_single_turn and isinstance(num_turns, int) and num_turns != 1:
            raise RuntimeError(
                f"AntigravityCLIAbductionBackend expected single-turn pure JSON abduction "
                f"(num_turns=1), got num_turns={num_turns}; refusing tool-assisted output"
            )

        cid = envelope.get("conversation_id")
        if isinstance(cid, str) and cid:
            self.last_conversation_id = cid
            if self.reuse_conversation and round_index >= 1:
                self._conversation_id = cid
        usage = envelope.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else None
        dur = envelope.get("duration_seconds")
        self.last_duration_sec = float(dur) if isinstance(dur, (int, float)) else None

        response_text = envelope.get("response")
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError(f"agy CLI returned empty response field: {envelope!r}")
        return _extract_json_object(response_text)


_BUILDER_PERSONA_HEADER = (
    "You are Ontology Builder (本体建造师 · 驻场测绘官). "
    "Given external domain phenomena, scout reports, and requirement documents, "
    "propose a strict L1 zero-exec DeclarativeBlueprint JSON (and AdapterMappingSpec JSON when external_profile is provided). "
    "Output ONLY valid JSON matching the exact schema below (extra keys are forbidden). "
    "Never hardcode concrete instance IDs or business codes. If Symbolic Crucible refutes your proposal, "
    "fix every reported counterexample.\n"
    "SCHEMA_GUIDE:\n"
)

_EVOLVER_PERSONA_HEADER = (
    "You are Ontology Evolver (本体进化师 · 业务法医). "
    "Given a base DeclarativeBlueprint and an escalated M0 FrictionClusterSummary, "
    "propose a minimal, general EvolutionPatch JSON with any required FieldMigrationSpec and new GoldenEvalCase assertions. "
    "Output ONLY valid JSON matching the exact schema below (extra keys are forbidden). "
    "Never overfit to concrete instance IDs/codes and never break historical golden eval cases.\n"
    "SCHEMA_GUIDE:\n"
)

_L2_ARCHITECT_PERSONA_HEADER = (
    "You are Ontology L2 Architect (本体结构与业务规则架构师). "
    "Given L2EvolutionTickets (structural expressivity/capability/settlement gaps), the current "
    "DeclarativeBlueprint + AdapterMappingSpec, and external domain phenomenon context, propose a pure M1 "
    "EvolutionPatch JSON adding new M1 fields/relations/actions (using safe 'if_else'/arithmetic expressions "
    "and preconditions), FieldMigrations, update_entity_mappings, declarative 1-to-1 "
    "add_action_bindings/update_action_bindings (declaring extra_params when actions take extra arguments), "
    "and self-contained ProposedEvalCase assertions. Output ONLY valid JSON matching the exact schema below.\n"
    "Never embed external wire/ORM protocols, concrete instance IDs, or break historical eval cases.\n"
    "SCHEMA_GUIDE:\n"
)


def build_builder_system_prompt(
    *,
    include_adapter_schema: bool = True,
    seed_ontology: SeedOntology | None = None,
) -> str:
    guide = dsl_schema_prompt_guide(mode="genesis", seed_ontology=seed_ontology)
    if include_adapter_schema:
        adapter_schema = json.dumps(
            AdapterMappingSpec.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"{_BUILDER_PERSONA_HEADER}{guide}\nADAPTER_MAPPING_SCHEMA:\n{adapter_schema}"
    return f"{_BUILDER_PERSONA_HEADER}{guide}"


def build_evolver_system_prompt(*, seed_ontology: SeedOntology | None = None) -> str:
    return f"{_EVOLVER_PERSONA_HEADER}{dsl_schema_prompt_guide(mode='evolution', seed_ontology=seed_ontology)}"


def build_l2_architect_system_prompt(*, seed_ontology: SeedOntology | None = None) -> str:
    guide = dsl_schema_prompt_guide(mode="evolution", seed_ontology=seed_ontology)
    adapter_schema = json.dumps(
        AdapterMappingSpec.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_L2_ARCHITECT_PERSONA_HEADER}{guide}\nADAPTER_MAPPING_SCHEMA:\n{adapter_schema}"


BUILDER_SYSTEM_PROMPT = build_builder_system_prompt(include_adapter_schema=True)
EVOLVER_SYSTEM_PROMPT = build_evolver_system_prompt()
L2_ARCHITECT_SYSTEM_PROMPT = build_l2_architect_system_prompt()


@dataclass(frozen=True)
class InductionRoundTrace:
    round_index: int
    proposal_id: str
    report_id: str
    passed: bool
    failed_gates: tuple[str, ...]
    feedback: str


@dataclass(frozen=True)
class InductionCycleResult:
    agent_persona: str
    proposal_id: str
    verified: bool
    pending_approval_run_id: str | None
    rounds: tuple[InductionRoundTrace, ...]
    final_report: CrucibleReport
    candidate_blueprint: DeclarativeBlueprint | None
    patch: EvolutionPatch | None = None
    adapter_mapping: AdapterMappingSpec | None = None
    projected_m0_objects: tuple[dict[str, Any], ...] = ()


class OntologyBuilderAgent:
    """Anthropomorphized 0->1 Ontology Builder running fault-tolerant CEGIS against SymbolicCrucible."""

    persona_name: str = "Ontology Builder (本体建造师)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = BUILDER_ACTOR,
    ) -> None:
        self.backend = backend
        self.governance = governance
        self.crucible = crucible or SymbolicCrucible()
        self.seed_ontology = seed_ontology
        self.actor = actor

    def run_genesis_cycle(
        self,
        *,
        model_id: str,
        phenomenon_brief: dict[str, Any] | None = None,
        crucible_context: CrucibleContext,
        requirement_doc: str = "",
        external_profile: ExternalPhenomenonProfile | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
    ) -> InductionCycleResult:
        if crucible_context.mode != "genesis":
            raise ValueError("OntologyBuilderAgent requires crucible_context.mode == 'genesis'")

        active_seed = seed_ontology or crucible_context.seed_ontology or self.seed_ontology
        pid = proposal_id or f"PROP-GEN-{model_id}-{uuid.uuid4().hex[:6]}"
        sys_prompt = build_builder_system_prompt(
            include_adapter_schema=external_profile is not None,
            seed_ontology=active_seed,
        )
        traces: list[InductionRoundTrace] = []
        last_feedback = ""
        last_report: CrucibleReport | None = None
        last_blueprint: DeclarativeBlueprint | None = None
        last_mapping: AdapterMappingSpec | None = None
        last_projected_m0: list[dict[str, Any]] = []
        pending_run_id: str | None = None
        proposal_created = False

        for r in range(1, max_rounds + 1):
            prompt_payload: dict[str, Any] = {
                "model_id": model_id,
                "round": r,
                "requirement_doc": requirement_doc,
                "phenomenon": phenomenon_brief or {},
                "previous_crucible_feedback": last_feedback,
            }
            if active_seed is not None:
                prompt_payload["seed_ontology"] = active_seed.prompt_summary()
            if external_profile is not None:
                prompt_payload["external_profile"] = external_profile.summary_for_builder()

            rid = f"RPT-{pid}-R{r}"
            try:
                raw_proposal = self.backend.propose_json(
                    system_prompt=sys_prompt,
                    user_prompt=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
                    round_index=r,
                )
                if not isinstance(raw_proposal, dict):
                    raise ValueError(f"expected JSON object from backend, got {type(raw_proposal).__name__}")
                bp_dict = raw_proposal.get("blueprint", raw_proposal)
                eval_cases = [
                    ProposedEvalCase.model_validate(c).model_dump(mode="json")
                    for c in (raw_proposal.get("proposed_eval_cases") or [])
                ]
                rationale = str(raw_proposal.get("rationale") or f"genesis round {r}")
                blueprint = DeclarativeBlueprint.model_validate(bp_dict)
                raw_mapping = raw_proposal.get("adapter_mapping")
                mapping = (
                    AdapterMappingSpec.model_validate(raw_mapping)
                    if raw_mapping is not None
                    else None
                )
            except Exception as exc:
                # P0-1 fix: capture parse/schema errors and feed back into next CEGIS round
                report = self.crucible.record_parse_failure(
                    self.governance,
                    proposal_id=pid,
                    report_id=rid,
                    mode="genesis",
                    model_id=model_id,
                    version="0.0.0",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                last_report = report
                feedback = report.diagnostic_feedback()
                traces.append(
                    InductionRoundTrace(
                        round_index=r,
                        proposal_id=pid,
                        report_id=rid,
                        passed=False,
                        failed_gates=tuple(g.gate for g in report.failed_gates()),
                        feedback=feedback,
                    )
                )
                last_feedback = feedback
                continue

            last_blueprint = blueprint
            if active_seed is not None and blueprint.seed_ontology_id is None:
                blueprint.seed_ontology_id = active_seed.seed_id
            mapping_json = "{}"
            round_ctx = copy.copy(crucible_context)
            round_ctx.seed_ontology = active_seed
            round_ctx.extra_gate2_errors = list(crucible_context.extra_gate2_errors)
            round_ctx.proposed_eval_cases = [*crucible_context.proposed_eval_cases, *eval_cases]
            round_ctx.adapter_mapping = mapping

            if external_profile is not None:
                if mapping is None:
                    round_ctx.extra_gate2_errors.append(
                        "MISSING_ADAPTER_MAPPING: Builder must emit adapter_mapping when external_profile is supplied"
                    )
                    last_projected_m0 = []
                else:
                    last_mapping = mapping
                    mapping_json = mapping.model_dump_json()
                    projected_objs, proj_errors = project_external_phenomenon(
                        external_profile, mapping
                    )
                    last_projected_m0 = projected_objs
                    round_ctx.m0_objects = [*crucible_context.m0_objects, *projected_objs]
                    round_ctx.extra_gate2_errors.extend(proj_errors)
            elif mapping is not None:
                last_mapping = mapping
                mapping_json = mapping.model_dump_json()

            bp_json = blueprint.model_dump_json()
            cases_json = json.dumps(eval_cases, ensure_ascii=False)
            if not proposal_created:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "propose_genesis_model",
                    {
                        "proposal_id": pid,
                        "model_id": blueprint.model_id,
                        "target_version": blueprint.version,
                        "title": f"Genesis {blueprint.model_id} v{blueprint.version}",
                        "rationale": rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )
                proposal_created = True
            else:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "revise_proposal",
                    {
                        "proposal_id": pid,
                        "rationale": rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )

            # P2-3 fix: Crucible independently evaluates & signs FalsificationReport under its own actor
            report = self.crucible.verify_and_record(
                self.governance,
                proposal_id=pid,
                report_id=rid,
                candidate=blueprint,
                context=round_ctx,
            )
            last_report = report
            feedback = report.diagnostic_feedback()
            traces.append(
                InductionRoundTrace(
                    round_index=r,
                    proposal_id=pid,
                    report_id=rid,
                    passed=report.passed,
                    failed_gates=tuple(g.gate for g in report.failed_gates()),
                    feedback=feedback,
                )
            )
            last_feedback = feedback

            if report.passed:
                pending_run_id = f"CRYSTAL-{pid}"
                self.governance.runtime.submit_action(
                    pending_run_id,
                    "crystallize_model",
                    {"proposal_id": pid, "approval_note": f"verified in round {r}"},
                    self.actor,
                )
                break

        assert last_report is not None
        return InductionCycleResult(
            agent_persona=self.persona_name,
            proposal_id=pid,
            verified=last_report.passed,
            pending_approval_run_id=pending_run_id,
            rounds=tuple(traces),
            final_report=last_report,
            candidate_blueprint=last_blueprint,
            adapter_mapping=last_mapping,
            projected_m0_objects=tuple(last_projected_m0),
        )


class OntologyEvolverAgent:
    """Anthropomorphized 1->N Ontology Evolver running fault-tolerant CEGIS against SymbolicCrucible."""

    persona_name: str = "Ontology Evolver (本体进化师)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = EVOLVER_ACTOR,
    ) -> None:
        self.backend = backend
        self.governance = governance
        self.crucible = crucible or SymbolicCrucible()
        self.seed_ontology = seed_ontology
        self.actor = actor

    def run_evolution_cycle(
        self,
        *,
        model_id: str,
        cluster: FrictionClusterSummary,
        crucible_context: CrucibleContext,
        base_blueprint: DeclarativeBlueprint | None = None,
        base_mapping: AdapterMappingSpec | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
        system_prompt_override: str | None = None,
        extra_prompt_context: dict[str, Any] | None = None,
        l2_validation_hook: Callable[
            [DeclarativeBlueprint, AdapterMappingSpec | None], list[str]
        ] | None = None,
    ) -> InductionCycleResult:
        if crucible_context.mode != "evolution":
            raise ValueError("OntologyEvolverAgent requires crucible_context.mode == 'evolution'")
        if not cluster.escalated:
            raise InvariantError(
                f"friction cluster {cluster.cluster_id!r} (support={cluster.support_count}) "
                "has not met escalation threshold; refusing to trigger evolution on noise",
                code="BELOW_ESCALATION_THRESHOLD",
            )

        base = base_blueprint or self.governance.get_active_blueprint(model_id)
        if base is None:
            raise InvariantError(
                f"no active crystallized blueprint found for model {model_id!r}",
                code="NO_ACTIVE_MODEL",
            )
        active_base_mapping = (
            base_mapping
            or crucible_context.adapter_mapping
            or self.governance.get_active_adapter_mapping(model_id)
        )

        # Record friction cluster under crucible's own actor
        self.governance.runtime.submit_action(
            f"FRC-REC-{cluster.cluster_id}-{uuid.uuid4().hex[:6]}",
            "record_friction_cluster",
            {
                "cluster_id": cluster.cluster_id,
                "model_id": model_id,
                "signal_kind": cluster.signal_kind,
                "action_type": cluster.action_type,
                "signature": cluster.signature,
                "support_count": cluster.support_count,
                "run_ids_json": json.dumps(list(cluster.run_ids), ensure_ascii=False),
            },
            self.crucible._actor,
        )

        active_seed = seed_ontology or crucible_context.seed_ontology or self.seed_ontology
        merged_ctx = copy.copy(crucible_context)
        merged_ctx.seed_ontology = active_seed
        existing_golden = self.governance.get_golden_eval_cases(model_id)
        seen_names = {str(c.get("name")) for c in merged_ctx.golden_eval_cases}
        combined_golden = list(merged_ctx.golden_eval_cases)
        for gc in existing_golden:
            if str(gc.get("name")) not in seen_names:
                combined_golden.append(gc)
                seen_names.add(str(gc.get("name")))
        merged_ctx.golden_eval_cases = combined_golden

        pid = proposal_id or f"PROP-EVO-{model_id}-{uuid.uuid4().hex[:6]}"
        sys_prompt = system_prompt_override or build_evolver_system_prompt(seed_ontology=active_seed)
        traces: list[InductionRoundTrace] = []
        last_feedback = ""
        last_report: CrucibleReport | None = None
        last_blueprint: DeclarativeBlueprint = base
        last_patch: EvolutionPatch | None = None
        last_mapping: AdapterMappingSpec | None = active_base_mapping
        pending_run_id: str | None = None
        proposal_created = False

        for r in range(1, max_rounds + 1):
            prompt_payload: dict[str, Any] = {
                "model_id": model_id,
                "round": r,
                "base_version": base.version,
                "base_blueprint": base.model_dump(mode="json"),
                "friction_cluster": {
                    "cluster_id": cluster.cluster_id,
                    "signal_kind": cluster.signal_kind,
                    "action_type": cluster.action_type,
                    "signature": cluster.signature,
                    "support_count": cluster.support_count,
                    "evidences": [
                        {
                            "run_id": e.run_id,
                            "actor_role": e.actor_role,
                            "params": e.params,
                            "summary": e.summary,
                            "error_code": e.error_code,
                        }
                        for e in cluster.evidences
                    ],
                },
                "previous_crucible_feedback": last_feedback,
            }
            if active_base_mapping is not None:
                prompt_payload["base_adapter_mapping"] = active_base_mapping.model_dump(mode="json")
            if active_seed is not None:
                prompt_payload["seed_ontology"] = active_seed.prompt_summary()
            if extra_prompt_context:
                prompt_payload.update(extra_prompt_context)
            rid = f"RPT-{pid}-R{r}"
            try:
                raw_patch = self.backend.propose_json(
                    system_prompt=sys_prompt,
                    user_prompt=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
                    round_index=r,
                )
                if not isinstance(raw_patch, dict):
                    raise ValueError(f"expected JSON object from backend, got {type(raw_patch).__name__}")
                patch = EvolutionPatch.model_validate(raw_patch.get("patch", raw_patch))
                candidate = apply_evolution_patch(base, patch)
                updated_mapping = apply_adapter_mapping_patch(active_base_mapping, patch)
            except Exception as exc:
                # P0-1 fix: catch schema/patch errors and feed back into next CEGIS round
                report = self.crucible.record_parse_failure(
                    self.governance,
                    proposal_id=pid,
                    report_id=rid,
                    mode="evolution",
                    model_id=model_id,
                    version=base.version,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                last_report = report
                feedback = report.diagnostic_feedback()
                traces.append(
                    InductionRoundTrace(
                        round_index=r,
                        proposal_id=pid,
                        report_id=rid,
                        passed=False,
                        failed_gates=tuple(g.gate for g in report.failed_gates()),
                        feedback=feedback,
                    )
                )
                last_feedback = feedback
                continue

            last_patch = patch
            last_blueprint = candidate
            last_mapping = updated_mapping
            bp_json = candidate.model_dump_json()
            patch_json = patch.model_dump_json()
            mapping_json = updated_mapping.model_dump_json() if updated_mapping is not None else "{}"
            cases_json = json.dumps(
                [c.model_dump(mode="json") for c in patch.proposed_eval_cases],
                ensure_ascii=False,
            )
            if not proposal_created:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "propose_evolution_patch",
                    {
                        "proposal_id": pid,
                        "model_id": model_id,
                        "cluster_id": cluster.cluster_id,
                        "base_version": base.version,
                        "target_version": patch.target_version,
                        "title": f"Evolve {model_id} {base.version}->{patch.target_version}",
                        "rationale": patch.rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "patch_json": patch_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )
                proposal_created = True
            else:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "revise_proposal",
                    {
                        "proposal_id": pid,
                        "rationale": patch.rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "patch_json": patch_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )

            round_ctx = copy.copy(merged_ctx)
            round_ctx.adapter_mapping = updated_mapping
            round_ctx.extra_gate2_errors = list(merged_ctx.extra_gate2_errors)
            if l2_validation_hook is not None:
                round_ctx.extra_gate2_errors.extend(
                    l2_validation_hook(candidate, updated_mapping)
                )

            report = self.crucible.verify_and_record(
                self.governance,
                proposal_id=pid,
                report_id=rid,
                candidate=candidate,
                context=round_ctx,
                patch=patch,
            )
            last_report = report
            feedback = report.diagnostic_feedback()
            traces.append(
                InductionRoundTrace(
                    round_index=r,
                    proposal_id=pid,
                    report_id=rid,
                    passed=report.passed,
                    failed_gates=tuple(g.gate for g in report.failed_gates()),
                    feedback=feedback,
                )
            )
            last_feedback = feedback

            if report.passed:
                pending_run_id = f"CRYSTAL-{pid}"
                self.governance.runtime.submit_action(
                    pending_run_id,
                    "crystallize_model",
                    {"proposal_id": pid, "approval_note": f"verified in round {r}"},
                    self.actor,
                )
                break

        assert last_report is not None
        return InductionCycleResult(
            agent_persona=self.persona_name,
            proposal_id=pid,
            verified=last_report.passed,
            pending_approval_run_id=pending_run_id,
            rounds=tuple(traces),
            final_report=last_report,
            candidate_blueprint=last_blueprint,
            patch=last_patch,
            adapter_mapping=last_mapping,
        )


class OntologyL2ArchitectAgent:
    """Anthropomorphized 1->N L2 M1 structural & business-rule architect over SymbolicCrucible."""

    persona_name: str = "Ontology L2 Architect (本体结构与业务规则架构师)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = EVOLVER_ACTOR,
    ) -> None:
        self._evolver = OntologyEvolverAgent(
            backend=backend,
            governance=governance,
            crucible=crucible,
            seed_ontology=seed_ontology,
            actor=actor,
        )
        self._evolver.persona_name = self.persona_name

    def run_l2_evolution_cycle(
        self,
        *,
        model_id: str,
        l2_tickets: Sequence[L2EvolutionTicket],
        crucible_context: CrucibleContext,
        external_phenomenon_context: Mapping[str, Any] | None = None,
        base_blueprint: DeclarativeBlueprint | None = None,
        base_mapping: AdapterMappingSpec | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
        l2_validation_hook: Callable[
            [DeclarativeBlueprint, AdapterMappingSpec | None], list[str]
        ] | None = None,
    ) -> InductionCycleResult:
        if not l2_tickets:
            raise ValueError("OntologyL2ArchitectAgent requires at least one L2EvolutionTicket")

        cluster = FrictionClusterSummary(
            cluster_id=f"L2-STRUCT-{model_id}-{uuid.uuid4().hex[:6]}",
            signal_kind="L2_CAPABILITY_GAP",
            action_type=l2_tickets[0].capability_gap,
            signature="+".join(sorted({t.probe_id for t in l2_tickets})),
            support_count=max(2, len(l2_tickets)),
            run_ids=tuple(t.ticket_id for t in l2_tickets),
            escalated=True,
            evidences=(),
        )
        active_seed = (
            seed_ontology or crucible_context.seed_ontology or self._evolver.seed_ontology
        )
        sys_prompt = build_l2_architect_system_prompt(seed_ontology=active_seed)
        extra_ctx: dict[str, Any] = {
            "l2_tickets": [t.to_dict() for t in l2_tickets],
            "external_phenomenon_context": dict(external_phenomenon_context or {}),
        }
        return self._evolver.run_evolution_cycle(
            model_id=model_id,
            cluster=cluster,
            crucible_context=crucible_context,
            base_blueprint=base_blueprint,
            base_mapping=base_mapping,
            seed_ontology=active_seed,
            max_rounds=max_rounds,
            proposal_id=proposal_id or f"PROP-L2-{model_id}-{uuid.uuid4().hex[:6]}",
            system_prompt_override=sys_prompt,
            extra_prompt_context=extra_ctx,
            l2_validation_hook=l2_validation_hook,
        )
