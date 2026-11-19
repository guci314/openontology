"""无人 FDE 自主侦察器 · 探针 3（克隆隔离 Odoo 沙箱主动黑盒拷问 · 零硬编码 · 零污染生产）。

在无人可拷问的条件下，用 ``duplicate_database`` 克隆一次性隔离 Odoo 沙箱库（退出 ``finally`` 自动销毁，
对生产库 100% 零写入），基于探针 1+2 产出的 ``FDEScoutReport`` 自主完成：

1. **零硬编码公开动作发现**：从 ``ir.ui.view`` 表单 XML 解析 ``<button type="object" name="...">``，
   自动过滤导航/打印类按钮，定位每个 ``ProcessDocument`` 的真实状态变迁方法；
2. **正向因果与跨表副作用实验（$T_x(x_t, u_t)$）**：对各状态真实记录调用候选动作，对比调用前后
   本记录字段 Diff 及全子图各表行数增量，自动发现跨表联动副作用（如确认单据自动派生下游单据）
   以及需要向导弹窗的瞬态模型（``ir.actions.act_window``）；
3. **对抗边界实验（``PROVEN_SYSTEMIC`` 原生硬护栏）**：在非初始态/终态上试探调用动作，
   直接捕获目标系统原生抛出的业务校验异常，把克隆系统自身当作 100% 诚实的 Oracle。

运行方式::

    python3 -m examples.odoo_fde_prober --db erp_north
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .odoo_dual_erp_demo import (
    DB_NORTH,
    MASTER_CANDIDATES,
    OdooJSONRPC,
    OdooRPCError,
    _jsonrpc,
)
from .odoo_fde_scout import (
    FDEScoutReport,
    OdooFDEScout,
    ReadOnlyOdooClient,
)

# 仅包含通用 UI 导航/打印过滤词与通用生命周期动作词，严禁硬编码具体业务表或业务方法全名
_UI_ONLY_METHOD_PREFIXES = (
    "action_view_",
    "action_open_",
    "action_see_",
    "action_preview_",
    "action_show_",
    "action_print_",
    "do_print_",
    "action_add_from_",
    "action_quotation_send",
    "action_invoice_sent",
    "action_activate_",
    "action_update_",
    "action_delete_",
    "action_put_in_",
    "action_detailed_",
    "action_next_",
    "action_purchase_",
    "action_automatic_",
    "payment_action_",
)

_LIFECYCLE_METHOD_KEYWORDS = (
    "confirm",
    "post",
    "validate",
    "cancel",
    "draft",
    "assign",
    "approve",
    "done",
    "reverse",
    "register_payment",
    "lock",
    "unlock",
    "close",
    "reset",
)

_BUTTON_RE_A = re.compile(r'<button[^>]*\bname="([a-zA-Z0-9_]+)"[^>]*\btype="object"')
_BUTTON_RE_B = re.compile(r'<button[^>]*\btype="object"[^>]*\bname="([a-zA-Z0-9_]+)"')


class ProbeSandboxInterface(Protocol):
    def search_count(self, model: str, domain: list[Any]) -> int:
        ...

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        order: str = "",
    ) -> list[dict[str, Any]]:
        ...

    def call_method(self, model: str, method: str, record_ids: list[int]) -> Any:
        ...


class OdooSandboxClient:
    """Client bound strictly to an ephemeral cloned Odoo sandbox database."""

    def __init__(self, db: str, *, client: OdooJSONRPC | None = None) -> None:
        self.db = db
        self._client = client or OdooJSONRPC(db)
        self._logged_in = False

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._client.login()
            self._logged_in = True

    def search_count(self, model: str, domain: list[Any]) -> int:
        self._ensure_login()
        return int(self._client.execute_kw(model, "search_count", [domain]))

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        order: str = "",
    ) -> list[dict[str, Any]]:
        self._ensure_login()
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        return list(self._client.execute_kw(model, "search_read", [domain], kwargs))

    def call_method(self, model: str, method: str, record_ids: list[int]) -> Any:
        self._ensure_login()
        return self._client.execute_kw(model, method, [record_ids])


class EphemeralOdooSandbox:
    """Clones ``source_db`` into a throwaway Odoo DB and unconditionally drops it on exit."""

    def __init__(self, source_db: str = DB_NORTH) -> None:
        self.source_db = source_db
        self.clone_db = f"fde_probe_{uuid.uuid4().hex[:8]}"
        self._master_pw: str | None = None
        self.client: OdooSandboxClient | None = None

    def __enter__(self) -> OdooSandboxClient:
        last_err: Exception | None = None
        for pw in MASTER_CANDIDATES:
            if not pw:
                continue
            try:
                ok = _jsonrpc("db", "duplicate_database", [pw, self.source_db, self.clone_db])
                if ok:
                    self._master_pw = pw
                    self.client = OdooSandboxClient(self.clone_db)
                    return self.client
            except Exception as exc:
                last_err = exc
        raise OdooRPCError(
            f"failed to clone {self.source_db!r} into ephemeral sandbox: {last_err}"
        )

    def __exit__(self, *exc: Any) -> None:
        if self._master_pw:
            try:
                _jsonrpc("db", "drop", [self._master_pw, self.clone_db])
            except Exception:
                pass


@dataclass(frozen=True)
class ActionProbeObservation:
    model: str
    method: str
    record_id: int
    record_label: str
    status_field: str
    state_before: str
    state_after: str
    outcome: str  # "TRANSITION" | "WIZARD_REQUIRED" | "NO_OP" | "NATIVE_ERROR"
    field_changes: dict[str, list[Any]] = field(default_factory=dict)
    cross_model_row_deltas: dict[str, int] = field(default_factory=dict)
    wizard_model: str | None = None
    error_message: str | None = None
    epistemic_tier: str = "PROVEN_SYSTEMIC"


@dataclass(frozen=True)
class ActiveProbeReport:
    source_locator: str
    sandbox_db: str
    discovered_action_methods: dict[str, tuple[str, ...]]
    observations: tuple[ActionProbeObservation, ...]

    def causal_transitions(self) -> list[ActionProbeObservation]:
        return [o for o in self.observations if o.outcome == "TRANSITION"]

    def native_error_guards(self) -> list[ActionProbeObservation]:
        return [o for o in self.observations if o.outcome == "NATIVE_ERROR"]

    def cross_model_side_effects(self) -> list[ActionProbeObservation]:
        return [o for o in self.observations if o.cross_model_row_deltas]

    def wizard_interactions(self) -> list[ActionProbeObservation]:
        return [o for o in self.observations if o.outcome == "WIZARD_REQUIRED"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_locator": self.source_locator,
            "sandbox_db": self.sandbox_db,
            "discovered_action_methods": {
                m: list(ms) for m, ms in self.discovered_action_methods.items()
            },
            "summary": {
                "total_probes": len(self.observations),
                "causal_transitions": len(self.causal_transitions()),
                "cross_model_side_effects": len(self.cross_model_side_effects()),
                "wizard_interactions": len(self.wizard_interactions()),
                "native_error_guards": len(self.native_error_guards()),
            },
            "observations": [
                {
                    "model": o.model,
                    "method": o.method,
                    "record_id": o.record_id,
                    "record_label": o.record_label,
                    "status_field": o.status_field,
                    "state_before": o.state_before,
                    "state_after": o.state_after,
                    "outcome": o.outcome,
                    "field_changes": o.field_changes,
                    "cross_model_row_deltas": o.cross_model_row_deltas,
                    "wizard_model": o.wizard_model,
                    "error_message": o.error_message,
                    "epistemic_tier": o.epistemic_tier,
                }
                for o in self.observations
            ],
        }


def _extract_object_buttons(arch_xml: str) -> list[str]:
    names: list[str] = []
    for rx in (_BUTTON_RE_A, _BUTTON_RE_B):
        for match in rx.findall(arch_xml or ""):
            if match and match not in names:
                names.append(match)
    return names


def _rank_lifecycle_method(method_name: str) -> tuple[int, str] | None:
    low = method_name.lower()
    if any(low.startswith(p) for p in _UI_ONLY_METHOD_PREFIXES):
        return None
    for idx, kw in enumerate(_LIFECYCLE_METHOD_KEYWORDS):
        if kw in low:
            return (idx, method_name)
    return None


class OdooActiveProber:
    """Probe 3: zero-hardcoding active causal & boundary prober over an isolated cloned sandbox."""

    def __init__(
        self,
        sandbox: ProbeSandboxInterface,
        *,
        sandbox_db: str = "ephemeral-sandbox",
        max_methods_per_model: int = 4,
    ) -> None:
        self.sandbox = sandbox
        self.sandbox_db = sandbox_db
        self.max_methods_per_model = max_methods_per_model

    def discover_methods_from_views(self, process_models: list[str]) -> dict[str, tuple[str, ...]]:
        if not process_models:
            return {}
        views = self.sandbox.search_read(
            "ir.ui.view",
            [("model", "in", process_models), ("type", "=", "form")],
            ["model", "arch_db"],
            limit=80,
        )
        raw_by_model: dict[str, set[str]] = {m: set() for m in process_models}
        for v in views:
            m = str(v.get("model") or "")
            if m not in raw_by_model:
                continue
            for btn in _extract_object_buttons(str(v.get("arch_db") or "")):
                raw_by_model[m].add(btn)

        ranked_out: dict[str, tuple[str, ...]] = {}
        for m in process_models:
            scored = []
            for btn in raw_by_model.get(m, ()):
                rk = _rank_lifecycle_method(btn)
                if rk is not None:
                    scored.append(rk)
            scored.sort()
            ranked_out[m] = tuple(name for _, name in scored[: self.max_methods_per_model])
        return ranked_out

    def probe(self, scout_report: FDEScoutReport) -> ActiveProbeReport:
        process_docs = [
            dm for dm in scout_report.discovered_models if dm.suggested_archetype == "ProcessDocument"
        ]
        process_model_names = [dm.model for dm in process_docs]
        all_watched_models = [dm.model for dm in scout_report.discovered_models]
        methods_by_model = self.discover_methods_from_views(process_model_names)

        # Map each ProcessDocument to its primary status field & known initial/terminal states from Probe 2
        sm_by_model: dict[str, Any] = {}
        for sm in scout_report.mined_state_machines:
            if sm.model not in sm_by_model or sm.field_name == "state":
                sm_by_model[sm.model] = sm

        observations: list[ActionProbeObservation] = []

        for dm in process_docs:
            mname = dm.model
            methods = methods_by_model.get(mname, ())
            if not methods:
                continue
            sm = sm_by_model.get(mname)
            status_field = sm.field_name if sm is not None else next(
                (f.name for f in dm.retained_fields if f.role == "status_enum"),
                "state",
            )
            watch_fields = list(
                dict.fromkeys(
                    ["id", "name", status_field]
                    + [
                        f.name
                        for f in dm.retained_fields[:6]
                        if f.ttype in ("char", "selection", "monetary", "float", "integer", "boolean")
                    ]
                )
            )

            # Pick candidate states to probe: initial state(s) + 1 non-initial / terminal state
            target_states: list[str] = []
            if sm is not None:
                target_states.extend(sm.inferred_initial_states[:1])
                for ts in sm.inferred_terminal_states[:1]:
                    if ts not in target_states:
                        target_states.append(ts)
                # Also pick the most common destination state if no terminal state exists
                if len(target_states) < 2 and sm.edges:
                    top_dest = sm.edges[0].to_state
                    if top_dest not in target_states:
                        target_states.append(top_dest)

            # Collect distinct real records per target state so each probe uses a clean record
            records_by_state: dict[str, list[dict[str, Any]]] = {}
            for st in target_states:
                try:
                    recs = self.sandbox.search_read(
                        mname,
                        [(status_field, "=", st)],
                        watch_fields,
                        limit=max(len(methods) + 1, 4),
                        order="id desc",
                    )
                    if recs:
                        records_by_state[st] = recs
                except Exception:
                    continue

            if not records_by_state:
                try:
                    recs = self.sandbox.search_read(mname, [], watch_fields, limit=4, order="id desc")
                    if recs:
                        st_val = str(recs[0].get(status_field) or "unknown")
                        records_by_state[st_val] = recs
                except Exception:
                    continue

            for st, rec_pool in records_by_state.items():
                for idx, method in enumerate(methods):
                    if idx >= len(rec_pool):
                        break
                    rec = rec_pool[idx]
                    rec_id = int(rec["id"])
                    rec_label = str(rec.get("name") or rec_id)

                    # Re-read exact before state + global subgraph row counts
                    before_rows = self.sandbox.search_read(
                        mname, [("id", "=", rec_id)], watch_fields, limit=1
                    )
                    if not before_rows:
                        continue
                    before = before_rows[0]
                    state_before = str(before.get(status_field) or st)
                    counts_before = {
                        wm: self.sandbox.search_count(wm, []) for wm in all_watched_models
                    }

                    try:
                        ret = self.sandbox.call_method(mname, method, [rec_id])
                    except Exception as exc:
                        observations.append(
                            ActionProbeObservation(
                                model=mname,
                                method=method,
                                record_id=rec_id,
                                record_label=rec_label,
                                status_field=status_field,
                                state_before=state_before,
                                state_after=state_before,
                                outcome="NATIVE_ERROR",
                                error_message=str(exc).splitlines()[0][:240],
                            )
                        )
                        continue

                    after_rows = self.sandbox.search_read(
                        mname, [("id", "=", rec_id)], watch_fields, limit=1
                    )
                    after = after_rows[0] if after_rows else before
                    state_after = str(after.get(status_field) or state_before)

                    counts_after = {
                        wm: self.sandbox.search_count(wm, []) for wm in all_watched_models
                    }
                    deltas = {
                        wm: counts_after[wm] - counts_before[wm]
                        for wm in all_watched_models
                        if counts_after[wm] != counts_before[wm]
                    }

                    fchanges: dict[str, list[Any]] = {}
                    for k in watch_fields:
                        if before.get(k) != after.get(k):
                            fchanges[k] = [before.get(k), after.get(k)]

                    wizard_model: str | None = None
                    if isinstance(ret, dict) and ret.get("type") == "ir.actions.act_window" and ret.get("res_model"):
                        wizard_model = str(ret["res_model"])

                    if state_before != state_after or deltas or fchanges:
                        outcome = "TRANSITION"
                    elif wizard_model is not None:
                        outcome = "WIZARD_REQUIRED"
                    else:
                        outcome = "NO_OP"

                    observations.append(
                        ActionProbeObservation(
                            model=mname,
                            method=method,
                            record_id=rec_id,
                            record_label=rec_label,
                            status_field=status_field,
                            state_before=state_before,
                            state_after=state_after,
                            outcome=outcome,
                            field_changes=fchanges,
                            cross_model_row_deltas=deltas,
                            wizard_model=wizard_model,
                        )
                    )

        return ActiveProbeReport(
            source_locator=scout_report.source_locator,
            sandbox_db=self.sandbox_db,
            discovered_action_methods=methods_by_model,
            observations=tuple(observations),
        )


def format_probe_summary(report: ActiveProbeReport) -> str:
    lines = [
        f"=== Probe 3 · Active Cloned-Sandbox Interrogation ({report.source_locator} -> {report.sandbox_db}) ===",
        "Discovered object methods from ir.ui.view:",
    ]
    for m, ms in report.discovered_action_methods.items():
        lines.append(f"  - {m}: {list(ms)}")
    lines.append(
        f"\nObservations ({len(report.observations)} total | "
        f"transitions={len(report.causal_transitions())}, "
        f"cross_model_effects={len(report.cross_model_side_effects())}, "
        f"wizards={len(report.wizard_interactions())}, "
        f"native_errors={len(report.native_error_guards())}):"
    )
    for o in report.observations:
        extra = ""
        if o.cross_model_row_deltas:
            extra += f" | cross_deltas={o.cross_model_row_deltas}"
        if o.wizard_model:
            extra += f" | wizard={o.wizard_model}"
        if o.error_message:
            extra += f" | err={o.error_message}"
        lines.append(
            f"  - [{o.outcome}] {o.model}.{o.method}({o.record_label}) "
            f"{o.status_field}: {o.state_before} -> {o.state_after}{extra}"
        )
    return "\n".join(lines)


def discover_transient_wizards(
    sandbox: ProbeSandboxInterface,
    related_models: Sequence[str] = (),
    *,
    probe_report: ActiveProbeReport | None = None,
) -> list[dict[str, Any]]:
    """Inspect Odoo ir.model/ir.model.fields/ir.ui.view for transient wizard & child-line schemas."""
    wizard_names: list[str] = []
    wizard_labels: dict[str, str] = {}
    if probe_report is not None:
        for obs in probe_report.observations:
            if obs.wizard_model and obs.wizard_model not in wizard_names:
                wizard_names.append(obs.wizard_model)

    module_prefixes = {
        str(rm).split(".")[0] for rm in related_models if str(rm).strip()
    }
    generic_wizard_tokens = (
        "payment.register",
        "backorder",
        "reversal",
        "refund",
        "advance.payment",
    )
    try:
        rows = sandbox.search_read(
            "ir.model",
            [("transient", "=", True)],
            ["model", "name"],
            limit=200,
        )
        for row in rows:
            m = str(row.get("model") or "")
            if not m or m.endswith(".line"):
                continue
            prefix = m.split(".")[0]
            if module_prefixes and prefix not in module_prefixes:
                continue
            m_low = m.lower()
            if not any(tok in m_low for tok in generic_wizard_tokens):
                continue
            wizard_labels[m] = str(row.get("name") or m)
            if m not in wizard_names:
                wizard_names.append(m)
    except Exception:
        pass

    catalog: list[dict[str, Any]] = []
    for wm in wizard_names:
        fields_meta: list[dict[str, Any]] = []
        button_methods: list[str] = []
        try:
            frows = sandbox.search_read(
                "ir.model.fields",
                [("model", "=", wm), ("store", "=", True)],
                ["name", "ttype", "relation", "field_description"],
                limit=40,
            )
            for fr in frows:
                fn = str(fr.get("name") or "")
                if not fn or fn in ("create_uid", "create_date", "write_uid", "write_date", "id"):
                    continue
                fields_meta.append(
                    {
                        "name": fn,
                        "ttype": fr.get("ttype"),
                        "relation": fr.get("relation") or None,
                        "label": fr.get("field_description") or "",
                    }
                )
        except Exception:
            pass
        try:
            vrows = sandbox.search_read(
                "ir.ui.view",
                [("model", "=", wm), ("type", "=", "form")],
                ["arch_db"],
                limit=10,
            )
            seen_btn: set[str] = set()
            for vr in vrows:
                arch = str(vr.get("arch_db") or "")
                for pat in (_BUTTON_RE_A, _BUTTON_RE_B):
                    for m in pat.findall(arch):
                        if m not in seen_btn:
                            seen_btn.add(m)
                            button_methods.append(m)
        except Exception:
            pass
        catalog.append(
            {
                "wizard_model": wm,
                "label": wizard_labels.get(wm, wm),
                "fields": fields_meta,
                "button_methods": button_methods,
            }
        )

    # Generically inspect one2many child-line models in the discovered domain subgraph
    rel_list = [str(rm) for rm in related_models if str(rm).strip()]
    rel_set = set(rel_list)
    seen_child_pairs: set[tuple[str, str]] = set()
    if rel_list:
        try:
            o2m_rows = sandbox.search_read(
                "ir.model.fields",
                [("model", "in", rel_list), ("ttype", "=", "one2many")],
                ["model", "name", "relation"],
                limit=80,
            )
            for row in o2m_rows:
                parent_m = str(row.get("model") or "")
                child_m = str(row.get("relation") or "")
                o2m_fn = str(row.get("name") or "")
                if not parent_m or not child_m or child_m in rel_set:
                    continue
                if child_m.split(".")[0] != parent_m.split(".")[0]:
                    continue
                if child_m.count(".") > 2 or (parent_m, child_m) in seen_child_pairs:
                    continue
                try:
                    cfields = sandbox.search_read(
                        "ir.model.fields",
                        [
                            ("model", "=", child_m),
                            ("store", "=", True),
                            ("ttype", "in", ["float", "boolean", "many2one", "selection"]),
                        ],
                        ["name", "ttype", "relation", "field_description"],
                        limit=60,
                    )
                except Exception:
                    continue
                relevant = [
                    cf
                    for cf in cfields
                    if cf.get("relation") == parent_m
                    or "qty" in str(cf.get("name") or "").lower()
                    or "quantity" in str(cf.get("name") or "").lower()
                    or str(cf.get("name") or "") in ("picked", "state", "price_unit")
                ]
                has_qty_or_flag = any(
                    "qty" in str(cf.get("name") or "").lower()
                    or "quantity" in str(cf.get("name") or "").lower()
                    or str(cf.get("name") or "") == "picked"
                    for cf in relevant
                )
                if relevant and has_qty_or_flag:
                    seen_child_pairs.add((parent_m, child_m))
                    catalog.append(
                        {
                            "child_line_model": child_m,
                            "parent_model": parent_m,
                            "one2many_field": o2m_fn,
                            "fields": relevant,
                        }
                    )
        except Exception:
            pass

    return catalog


def run_live_autonomous_fde_discovery(
    *,
    db: str = DB_NORTH,
    task_intent: str = "",
    max_models: int = 6,
) -> dict[str, Any]:
    """Run full 3-probe autonomous FDE discovery (Probe 1+2 read-only on db + Probe 3 on throwaway clone)."""
    ro_client = ReadOnlyOdooClient(db=db)
    scout_report = OdooFDEScout(
        ro_client,
        source_locator=f"odoo://{db}",
        max_models=max_models,
    ).scout(task_intent=task_intent)

    with EphemeralOdooSandbox(source_db=db) as sandbox_client:
        probe_report = OdooActiveProber(
            sandbox_client,
            sandbox_db=sandbox_client.db,
        ).probe(scout_report)

    return {
        "scout_report": scout_report.to_dict(),
        "active_probe_report": probe_report.to_dict(),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous FDE Probe 1+2 (Read-Only) + Probe 3 (Cloned Sandbox Active Probing)"
    )
    parser.add_argument("--db", default=DB_NORTH, help=f"Source Odoo DB (default: {DB_NORTH})")
    parser.add_argument("--intent", default="", help="Optional task intent")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")
    args = parser.parse_args(argv)

    ro_client = ReadOnlyOdooClient(db=args.db)
    scout_report = OdooFDEScout(ro_client, source_locator=f"odoo://{args.db}").scout(
        task_intent=args.intent
    )
    with EphemeralOdooSandbox(source_db=args.db) as sandbox_client:
        probe_report = OdooActiveProber(sandbox_client, sandbox_db=sandbox_client.db).probe(
            scout_report
        )

    if args.as_json:
        print(
            json.dumps(
                {
                    "scout_report": scout_report.to_dict(),
                    "active_probe_report": probe_report.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_probe_summary(probe_report))


if __name__ == "__main__":
    main()
