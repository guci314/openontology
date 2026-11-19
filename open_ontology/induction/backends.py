"""可插拔溯因传输后端（脱离纯符号编排内核，集中隔离 HTTP / CLI 传输 I/O）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

__all__ = [
    "AbductionBackend",
    "CallableAbductionBackend",
    "GeminiAbductionBackend",
    "OpenAICompatibleAbductionBackend",
    "OpenRouterAbductionBackend",
    "AntigravityCLIAbductionBackend",
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
