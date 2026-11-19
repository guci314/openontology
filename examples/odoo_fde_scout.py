"""无人 FDE 自主侦察器（Probe 1 + Probe 2 · 纯只读 · 零硬编码业务表/字段名）。

不依赖人类实时拷问、不预设任何业务表名或字段清单，直接面向未知 Odoo 实例执行：

1. **探针 1 · 无硬编码活跃子图发现与信息熵裁剪（Schema & Entropy Pruner）**：
   * 扫描 ``ir.model`` 与 ``mail.message`` / ``mail.tracking.value`` 活跃度（可选叠加自然语言任务意图词匹配），
     自动定位高活跃业务锚点，并沿 ``ir.model.fields`` 的 ``many2one`` 外键做 1 跳邻域展开；
   * 对子图内每个模型采样真实行，计算各字段非空率与香农信息熵 $H(f)$，自动裁剪零行、零熵常量
     与框架技术列，输出压缩比与建议原型（``ProcessDocument`` / ``MasterEntity`` / ``EventVoucher``）。
2. **探针 2 · 历史轨迹与系统规约考古（Trace & Artifact Archaeologist）**：
   * **状态机挖掘（``INFERRED_TRACE``）**：直读 ``mail.tracking.value`` 聚合状态跃迁边、初始态、终态与回退循环；
   * **系统硬约束与权限（``PROVEN_SYSTEMIC``）**：直读 ``ir.model.constraint`` 唯一/检查约束与 ``ir.rule`` 行级安全规则；
   * **隐藏字符串外键发现**：通过实测值集合包含率，自动发现未声明为 ``many2one`` 的跨表字符串关联；
   * **多态混合表判别器发现**：自动检测单个物理表内不同分类枚举值导致条件非空率剧烈分化的混合表；
   * **未决灰区与矛盾挖掘（``AMBIGUOUS_GRAY_ZONE``）**：自动挖掘空外键孤儿集、跨表额度越界矛盾与高频撤销/冲正循环，
     在无人场景下给出保守默认策略（``PENDING_APPROVAL`` / ``FILTER_BY_DISCRIMINATOR``）。

运行方式::

    python3 -m examples.odoo_fde_scout --db erp_north
    python3 -m examples.odoo_fde_scout --db erp_north --intent "sales order delivery invoice payment" --json
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC

# ---------------------------------------------------------------------------
# 通用 Odoo 框架黑名单（仅过滤技术基础设施，严禁出现任何业务表名或业务字段名）
# ---------------------------------------------------------------------------
_FRAMEWORK_MODEL_PREFIXES = (
    "ir.",
    "base.",
    "bus.",
    "web.",
    "mail.",
    "report.",
    "digest.",
    "iap.",
    "sms.",
    "auth_",
    "onboarding.",
    "spreadsheet.",
    "utm.",
    "discuss.",
    "calendar.",
    "fetchmail.",
    "decimal.",
    "resource.",
    "barcode.",
    "format.",
    "html.",
    "portal.",
    "privacy.",
    "snailmail.",
    "uom.",
)

_FRAMEWORK_EXACT_MODELS = frozenset(
    {
        "res.users",
        "res.users.log",
        "res.users.settings",
        "res.groups",
        "res.company",
        "res.currency",
        "res.currency.rate",
        "res.country",
        "res.country.state",
        "res.country.group",
        "res.lang",
        "res.bank",
        "res.partner.bank",
        "res.partner.category",
        "res.partner.title",
        "res.partner.industry",
        "res.config",
        "res.config.settings",
    }
)

_TECH_FIELD_PREFIXES = (
    "message_",
    "activity_",
    "website_",
    "avatar_",
    "image_",
    "my_activity_",
    "rating_",
    "access_",
    "l10n_",
    "edi_",
    "peppol_",
    "ubl_",
    "__last_update",
)

_TECH_EXACT_FIELDS = frozenset(
    {
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "display_name",
        "has_message",
        "color",
        "sequence",
        "tz",
        "tz_offset",
        "lang",
        "commercial_partner_id",
        "commercial_company_name",
        "complete_name",
        "active_lang_count",
        "partner_share",
        "user_id",
    }
)

_LIMIT_FIELD_KEYWORDS = ("limit", "max", "cap", "quota", "ceiling", "threshold", "credit")
_MEASURE_FIELD_KEYWORDS = ("amount", "total", "residual", "balance", "price", "cost", "qty", "quantity")
_REVERSAL_KEYWORDS = ("cancel", "refund", "reverse", "credit", "void", "draft")


# ---------------------------------------------------------------------------
# Strictly Read-Only Odoo Interface (Physically Blocks Any Write Method)
# ---------------------------------------------------------------------------
class ReadOnlyOdooInterface(Protocol):
    def search_count(self, model: str, domain: list[Any]) -> int:
        ...

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 80,
        order: str = "",
    ) -> list[dict[str, Any]]:
        ...

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        lazy: bool = True,
    ) -> list[dict[str, Any]]:
        ...

    def fields_get(
        self,
        model: str,
        fields: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        ...


class ReadOnlyOdooClient:
    """Read-only wrapper over OdooJSONRPC; physically exposes NO create/write/unlink."""

    def __init__(self, db: str = DB_NORTH, *, client: OdooJSONRPC | None = None) -> None:
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
        limit: int = 80,
        order: str = "",
    ) -> list[dict[str, Any]]:
        self._ensure_login()
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        return list(self._client.execute_kw(model, "search_read", [domain], kwargs))

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        lazy: bool = True,
    ) -> list[dict[str, Any]]:
        self._ensure_login()
        return list(
            self._client.execute_kw(
                model,
                "read_group",
                [domain],
                {"fields": fields, "groupby": groupby, "lazy": lazy},
            )
        )

    def fields_get(
        self,
        model: str,
        fields: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self._ensure_login()
        args = [fields] if fields is not None else []
        kwargs = {"attributes": attributes} if attributes is not None else {}
        return dict(self._client.execute_kw(model, "fields_get", args, kwargs))


# ---------------------------------------------------------------------------
# Structured Scout Report Data Models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSignal:
    name: str
    label: str
    ttype: str
    role: str
    non_null_rate: float
    distinct_count: int
    entropy_bits: float
    relation_target: str | None = None
    selection_values: tuple[str, ...] = ()
    tracked_transitions: int = 0


@dataclass(frozen=True)
class DiscoveredModel:
    model: str
    label: str
    row_count: int
    message_count: int
    tracked_transition_count: int
    suggested_archetype: str
    total_stored_fields: int
    retained_fields: tuple[FieldSignal, ...]
    pruned_field_count: int
    rls_rule_count: int = 0


@dataclass(frozen=True)
class DiscoveredRelation:
    source_model: str
    source_field: str
    target_model: str
    target_field: str
    kind: str  # "explicit_many2one" | "inferred_string_join"
    confidence: float
    null_rate_in_sample: float


@dataclass(frozen=True)
class MinedTransitionEdge:
    from_state: str
    to_state: str
    from_label: str
    to_label: str
    count: int


@dataclass(frozen=True)
class MinedStateMachine:
    model: str
    field_name: str
    field_label: str
    epistemic_tier: str  # "INFERRED_TRACE"
    total_events: int
    edges: tuple[MinedTransitionEdge, ...]
    inferred_initial_states: tuple[str, ...]
    inferred_terminal_states: tuple[str, ...]
    backward_or_reversal_edges: tuple[str, ...]


@dataclass(frozen=True)
class SystemicConstraint:
    model: str
    name: str
    constraint_type: str
    definition: str
    message: str
    epistemic_tier: str = "PROVEN_SYSTEMIC"


@dataclass(frozen=True)
class PolymorphicDiscriminator:
    model: str
    field_name: str
    value_counts: dict[str, int]
    nullity_divergence_score: float
    divergent_columns: tuple[str, ...]
    epistemic_tier: str = "INFERRED_TRACE"


@dataclass(frozen=True)
class EpistemicFinding:
    finding_id: str
    epistemic_tier: str  # "PROVEN_SYSTEMIC" | "INFERRED_TRACE" | "AMBIGUOUS_GRAY_ZONE"
    kind: str
    model: str
    summary: str
    evidence: dict[str, Any]
    conservative_default: str


@dataclass(frozen=True)
class FDEScoutReport:
    source_locator: str
    task_intent: str
    total_models_scanned: int
    total_fields_scanned: int
    total_tracking_rows: int
    retained_model_count: int
    retained_field_count: int
    field_compression_ratio: float
    discovered_models: tuple[DiscoveredModel, ...]
    discovered_relations: tuple[DiscoveredRelation, ...]
    mined_state_machines: tuple[MinedStateMachine, ...]
    systemic_constraints: tuple[SystemicConstraint, ...]
    polymorphic_discriminators: tuple[PolymorphicDiscriminator, ...]
    epistemic_findings: tuple[EpistemicFinding, ...]
    sampled_rows_by_model: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_locator": self.source_locator,
            "task_intent": self.task_intent,
            "compression_summary": {
                "total_models_scanned": self.total_models_scanned,
                "retained_model_count": self.retained_model_count,
                "total_fields_scanned": self.total_fields_scanned,
                "retained_field_count": self.retained_field_count,
                "field_compression_ratio": self.field_compression_ratio,
                "total_tracking_rows": self.total_tracking_rows,
            },
            "discovered_models": [
                {
                    "model": m.model,
                    "label": m.label,
                    "row_count": m.row_count,
                    "message_count": m.message_count,
                    "tracked_transition_count": m.tracked_transition_count,
                    "suggested_archetype": m.suggested_archetype,
                    "total_stored_fields": m.total_stored_fields,
                    "retained_field_count": len(m.retained_fields),
                    "pruned_field_count": m.pruned_field_count,
                    "rls_rule_count": m.rls_rule_count,
                    "retained_fields": [
                        {
                            "name": f.name,
                            "label": f.label,
                            "ttype": f.ttype,
                            "role": f.role,
                            "non_null_rate": f.non_null_rate,
                            "distinct_count": f.distinct_count,
                            "entropy_bits": f.entropy_bits,
                            "relation_target": f.relation_target,
                            "selection_values": list(f.selection_values),
                            "tracked_transitions": f.tracked_transitions,
                        }
                        for f in m.retained_fields
                    ],
                }
                for m in self.discovered_models
            ],
            "discovered_relations": [
                {
                    "source": f"{r.source_model}.{r.source_field}",
                    "target": f"{r.target_model}.{r.target_field}",
                    "kind": r.kind,
                    "confidence": r.confidence,
                    "null_rate_in_sample": r.null_rate_in_sample,
                }
                for r in self.discovered_relations
            ],
            "mined_state_machines": [
                {
                    "model": sm.model,
                    "field": sm.field_name,
                    "label": sm.field_label,
                    "epistemic_tier": sm.epistemic_tier,
                    "total_events": sm.total_events,
                    "inferred_initial_states": list(sm.inferred_initial_states),
                    "inferred_terminal_states": list(sm.inferred_terminal_states),
                    "backward_or_reversal_edges": list(sm.backward_or_reversal_edges),
                    "edges": [
                        {
                            "from": e.from_state,
                            "to": e.to_state,
                            "from_label": e.from_label,
                            "to_label": e.to_label,
                            "count": e.count,
                        }
                        for e in sm.edges
                    ],
                }
                for sm in self.mined_state_machines
            ],
            "systemic_constraints": [
                {
                    "model": c.model,
                    "name": c.name,
                    "type": c.constraint_type,
                    "definition": c.definition,
                    "message": c.message,
                    "epistemic_tier": c.epistemic_tier,
                }
                for c in self.systemic_constraints
            ],
            "polymorphic_discriminators": [
                {
                    "model": p.model,
                    "field": p.field_name,
                    "value_counts": dict(p.value_counts),
                    "nullity_divergence_score": p.nullity_divergence_score,
                    "divergent_columns": list(p.divergent_columns),
                    "epistemic_tier": p.epistemic_tier,
                }
                for p in self.polymorphic_discriminators
            ],
            "epistemic_findings": [
                {
                    "finding_id": ef.finding_id,
                    "epistemic_tier": ef.epistemic_tier,
                    "kind": ef.kind,
                    "model": ef.model,
                    "summary": ef.summary,
                    "evidence": ef.evidence,
                    "conservative_default": ef.conservative_default,
                }
                for ef in self.epistemic_findings
            ],
        }


# ---------------------------------------------------------------------------
# Generic Utility Functions (Entropy, Normalization, Token Match)
# ---------------------------------------------------------------------------
def _is_framework_model(model_name: str) -> bool:
    if model_name in _FRAMEWORK_EXACT_MODELS:
        return True
    return any(model_name.startswith(p) for p in _FRAMEWORK_MODEL_PREFIXES)


def _is_tech_field(field_name: str) -> bool:
    if field_name in _TECH_EXACT_FIELDS:
        return True
    return any(field_name.startswith(p) for p in _TECH_FIELD_PREFIXES)


def _normalize_cell(val: Any, ttype: str) -> Any:
    if val is None:
        return None
    if ttype != "boolean" and val is False:
        return None
    if ttype == "many2one" and isinstance(val, (list, tuple)) and val:
        return val[0]
    if isinstance(val, str) and val.strip() == "":
        return None
    return val


def _shannon_entropy(values: Sequence[Any]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    counts = Counter(values)
    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log2(p)
    return round(entropy, 4)


def _tokenize_intent(intent: str) -> set[str]:
    if not intent.strip():
        return set()
    # Simple bilingual token map for common general business verbs/nouns (non-Odoo-specific)
    bilingual_hints = {
        "销售": ("sale", "order", "customer", "partner"),
        "订单": ("order", "sale"),
        "客户": ("partner", "customer", "contact"),
        "发货": ("picking", "delivery", "transfer", "stock", "shipment"),
        "出库": ("picking", "delivery", "stock", "transfer"),
        "库存": ("stock", "inventory", "quant", "warehouse"),
        "发票": ("invoice", "move", "account", "billing"),
        "开票": ("invoice", "move", "account"),
        "收款": ("payment", "invoice", "move", "receivable"),
        "付款": ("payment", "bill", "move"),
        "采购": ("purchase", "vendor", "supplier"),
        "生产": ("mrp", "production", "bom", "manufacturing"),
    }
    lowered = intent.lower()
    tokens = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for zh, en_words in bilingual_hints.items():
        if zh in intent:
            tokens.update(en_words)
    return tokens


def _classify_field_role(name: str, ttype: str, tracked_count: int) -> str:
    low = name.lower()
    if name in ("id", "name", "ref", "code") or low.endswith("_ref"):
        return "identifier"
    if ttype == "many2one":
        return "foreign_key"
    if ttype == "selection":
        if tracked_count > 0 or "state" in low or "status" in low:
            return "status_enum"
        return "category_enum"
    if ttype == "monetary" or any(k in low for k in _MEASURE_FIELD_KEYWORDS):
        return "measure"
    if any(k in low for k in _LIMIT_FIELD_KEYWORDS):
        return "limit_or_policy_attr"
    if ttype in ("date", "datetime"):
        return "timestamp"
    return "attribute"


# ---------------------------------------------------------------------------
# Autonomous FDE Scout (Probe 1 + Probe 2)
# ---------------------------------------------------------------------------
class OdooFDEScout:
    """Zero-hardcoding, read-only autonomous FDE scout for unknown Odoo instances."""

    def __init__(
        self,
        client: ReadOnlyOdooInterface,
        *,
        source_locator: str = "odoo://unknown",
        max_models: int = 6,
        sample_limit: int = 120,
        max_fields_per_model: int = 12,
        min_non_null_rate: float = 0.05,
    ) -> None:
        self.client = client
        self.source_locator = source_locator
        self.max_models = max_models
        self.sample_limit = sample_limit
        self.max_fields_per_model = max_fields_per_model
        self.min_non_null_rate = min_non_null_rate

    def scout(self, *, task_intent: str = "") -> FDEScoutReport:
        intent_tokens = _tokenize_intent(task_intent)

        # -------------------------------------------------------------------
        # Stage 1: Zero-Hardcoding Active Business Subgraph Discovery
        # -------------------------------------------------------------------
        all_models = self.client.search_read(
            "ir.model",
            [("transient", "=", False)],
            ["id", "model", "name"],
            limit=2000,
        )
        total_models_scanned = len(all_models)
        total_fields_scanned = self.client.search_count("ir.model.fields", [])
        total_tracking_rows = self.client.search_count("mail.tracking.value", [])

        model_info: dict[str, dict[str, Any]] = {
            str(m["model"]): {"id": int(m["id"]), "name": str(m.get("name") or m["model"])}
            for m in all_models
            if m.get("model")
        }

        # 1a. Activity via mail.message read_group
        msg_counts: dict[str, int] = {}
        try:
            mg = self.client.read_group(
                "mail.message",
                [("model", "!=", False)],
                ["model"],
                ["model"],
                lazy=True,
            )
            for row in mg:
                mname = str(row.get("model") or "")
                if mname:
                    msg_counts[mname] = int(row.get("model_count", row.get("__count", 0)))
        except Exception:
            pass

        # 1b. State/field mutation activity via mail.tracking.value read_group
        tv_by_field_id: dict[int, int] = {}
        try:
            tvg = self.client.read_group(
                "mail.tracking.value",
                [],
                ["field_id"],
                ["field_id"],
                lazy=True,
            )
            for row in tvg:
                fid = row.get("field_id")
                if isinstance(fid, (list, tuple)) and fid:
                    tv_by_field_id[int(fid[0])] = int(row.get("field_id_count", row.get("__count", 0)))
        except Exception:
            pass

        tracked_field_meta: dict[int, dict[str, Any]] = {}
        tracked_count_by_model: dict[str, int] = defaultdict(int)
        if tv_by_field_id:
            fmeta_rows = self.client.search_read(
                "ir.model.fields",
                [("id", "in", list(tv_by_field_id.keys()))],
                ["id", "name", "model", "field_description", "ttype"],
                limit=500,
            )
            for fm in fmeta_rows:
                fid = int(fm["id"])
                mname = str(fm["model"])
                cnt = tv_by_field_id.get(fid, 0)
                tracked_field_meta[fid] = {**fm, "count": cnt}
                tracked_count_by_model[mname] += cnt

        # 1c. Score non-framework candidate models purely by activity + optional intent match
        scored_candidates: list[tuple[float, str]] = []
        for mname, info in model_info.items():
            if _is_framework_model(mname):
                continue
            mc = msg_counts.get(mname, 0)
            tc = tracked_count_by_model.get(mname, 0)
            haystack = f"{mname} {info['name']}".lower()
            intent_hits = sum(1 for tok in intent_tokens if tok in haystack)
            if mc == 0 and tc == 0 and intent_hits == 0:
                continue
            score = math.log1p(mc) * 2.0 + math.log1p(tc) * 3.0 + intent_hits * 5.0
            scored_candidates.append((score, mname))

        scored_candidates.sort(key=lambda x: (-x[0], x[1]))

        # Pick top seed models with non-zero actual rows
        seed_models: list[str] = []
        row_counts: dict[str, int] = {}
        seed_budget = max(2, self.max_models - 2)
        for _score, mname in scored_candidates:
            if len(seed_models) >= seed_budget:
                break
            try:
                rc = self.client.search_count(mname, [])
            except Exception:
                continue
            if rc > 0:
                row_counts[mname] = rc
                seed_models.append(mname)

        # 1d. 1-hop foreign-key expansion via stored many2one fields in ir.model.fields
        selected_models = list(seed_models)
        selected_set = set(selected_models)
        if seed_models and len(selected_models) < self.max_models:
            fk_rows = self.client.search_read(
                "ir.model.fields",
                [
                    ("model", "in", seed_models),
                    ("ttype", "=", "many2one"),
                    ("store", "=", True),
                    ("relation", "!=", False),
                ],
                ["model", "name", "relation"],
                limit=1000,
            )
            rel_votes: Counter[str] = Counter()
            for r in fk_rows:
                target = str(r.get("relation") or "")
                fname = str(r.get("name") or "")
                if (
                    not target
                    or target in selected_set
                    or _is_framework_model(target)
                    or _is_tech_field(fname)
                    or target not in model_info
                ):
                    continue
                rel_votes[target] += 1
            for target, _votes in rel_votes.most_common():
                if len(selected_models) >= self.max_models:
                    break
                try:
                    rc = self.client.search_count(target, [])
                except Exception:
                    continue
                if rc > 0:
                    row_counts[target] = rc
                    selected_models.append(target)
                    selected_set.add(target)

        # -------------------------------------------------------------------
        # Stage 2: Entropy-Based Field Pruning & Row Sampling
        # -------------------------------------------------------------------
        stored_fields_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if selected_models:
            all_sf = self.client.search_read(
                "ir.model.fields",
                [("model", "in", selected_models), ("store", "=", True)],
                ["id", "model", "name", "field_description", "ttype", "relation", "required", "readonly"],
                limit=4000,
            )
            for sf in all_sf:
                stored_fields_by_model[str(sf["model"])].append(sf)

        # Count inbound many2one references across selected subgraph for archetype inference
        inbound_refs: Counter[str] = Counter()
        for mname in selected_models:
            for sf in stored_fields_by_model[mname]:
                if sf["ttype"] == "many2one" and sf.get("relation") in selected_set and sf["relation"] != mname:
                    if not _is_tech_field(str(sf["name"])):
                        inbound_refs[str(sf["relation"])] += 1

        # Fetch RLS rule counts per selected model
        rls_counts: Counter[str] = Counter()
        if selected_models:
            model_id_to_name = {model_info[m]["id"]: m for m in selected_models if m in model_info}
            try:
                rule_rows = self.client.search_read(
                    "ir.rule",
                    [("model_id", "in", list(model_id_to_name.keys())), ("active", "=", True)],
                    ["model_id"],
                    limit=500,
                )
                for rr in rule_rows:
                    mid = rr.get("model_id")
                    if isinstance(mid, (list, tuple)) and mid and int(mid[0]) in model_id_to_name:
                        rls_counts[model_id_to_name[int(mid[0])]] += 1
            except Exception:
                pass

        # Sample rows & prune low-entropy / framework fields per model
        sampled_rows_by_model: dict[str, list[dict[str, Any]]] = {}
        selection_maps_by_model: dict[str, dict[str, list[list[str]]]] = {}
        all_field_signals_by_model: dict[str, list[tuple[float, FieldSignal]]] = {}
        stored_count_by_model: dict[str, int] = {}
        explicit_relations: list[DiscoveredRelation] = []

        for mname in selected_models:
            raw_fields = stored_fields_by_model[mname]
            stored_count_by_model[mname] = len(raw_fields)
            # Keep scalar, selection, monetary, date, and subgraph-relevant many2one fields for sampling
            candidate_fields = [
                sf
                for sf in raw_fields
                if not _is_tech_field(str(sf["name"]))
                and sf["ttype"] in ("char", "integer", "float", "monetary", "boolean", "selection", "many2one", "date", "datetime")
                and (sf["ttype"] != "many2one" or sf.get("relation") in selected_set)
            ]
            field_names = [str(sf["name"]) for sf in candidate_fields]
            if "id" not in field_names:
                field_names.insert(0, "id")

            # Fetch selection label maps for selection fields
            sel_fields = [str(sf["name"]) for sf in candidate_fields if sf["ttype"] == "selection"]
            sel_map: dict[str, list[list[str]]] = {}
            if sel_fields:
                try:
                    fg = self.client.fields_get(mname, sel_fields, ["selection"])
                    for k, v in fg.items():
                        if isinstance(v.get("selection"), list):
                            sel_map[k] = [[str(p[0]), str(p[1])] for p in v["selection"] if len(p) >= 2]
                except Exception:
                    pass
            selection_maps_by_model[mname] = sel_map

            # Sample rows (most recent + earliest mix to catch historical diversity)
            rows: list[dict[str, Any]] = []
            try:
                recent = self.client.search_read(
                    mname, [], field_names, limit=self.sample_limit, order="id desc"
                )
                rows = recent
                if row_counts.get(mname, 0) > self.sample_limit:
                    older = self.client.search_read(
                        mname, [], field_names, limit=min(30, self.sample_limit // 3), order="id asc"
                    )
                    seen_ids = {r.get("id") for r in rows}
                    for o in older:
                        if o.get("id") not in seen_ids:
                            rows.append(o)
                            seen_ids.add(o.get("id"))
            except Exception:
                rows = []
            sampled_rows_by_model[mname] = rows
            n_rows = max(1, len(rows))

            # Score & prune fields by non-null rate, Shannon entropy, tracking count, and collinearity
            field_signals: list[tuple[float, FieldSignal]] = []
            seen_measure_vectors: dict[tuple[Any, ...], str] = {}
            # Sort candidate fields shortest-name first so canonical measure names win collinearity dedup
            for sf in sorted(candidate_fields, key=lambda s: (len(str(s["name"])), str(s["name"]))):
                fname = str(sf["name"])
                ttype = str(sf["ttype"])
                fid = int(sf["id"])
                tr_cnt = tv_by_field_id.get(fid, 0)
                norm_vals = [_normalize_cell(r.get(fname), ttype) for r in rows]
                non_null_vals = [v for v in norm_vals if v is not None]
                non_null_rate = round(len(non_null_vals) / n_rows, 4)
                distinct_cnt = len(set(non_null_vals))
                ent = _shannon_entropy(non_null_vals)

                if ttype == "many2one" and sf.get("relation") in selected_set and sf["relation"] != mname:
                    if non_null_rate > 0.0:
                        explicit_relations.append(
                            DiscoveredRelation(
                                source_model=mname,
                                source_field=fname,
                                target_model=str(sf["relation"]),
                                target_field="id",
                                kind="explicit_many2one",
                                confidence=1.0,
                                null_rate_in_sample=round(1.0 - non_null_rate, 4),
                            )
                        )

                is_anchor = fname in ("id", "name", "ref") or tr_cnt > 0
                if not is_anchor:
                    if non_null_rate < self.min_non_null_rate:
                        continue
                    if ent == 0.0 and not any(k in fname.lower() for k in _LIMIT_FIELD_KEYWORDS):
                        continue

                role = _classify_field_role(fname, ttype, tr_cnt)
                # Drop exact collinear duplicate measure columns (e.g. amount_total vs amount_total_signed)
                if role == "measure" and tr_cnt == 0 and len(rows) >= 5:
                    vec = tuple(round(abs(float(v)), 2) if isinstance(v, (int, float)) else None for v in norm_vals[:25])
                    if vec in seen_measure_vectors:
                        continue
                    seen_measure_vectors[vec] = fname

                sel_keys = tuple(p[0] for p in sel_map.get(fname, []))
                sig = FieldSignal(
                    name=fname,
                    label=str(sf.get("field_description") or fname),
                    ttype=ttype,
                    role=role,
                    non_null_rate=non_null_rate,
                    distinct_count=distinct_cnt,
                    entropy_bits=ent,
                    relation_target=str(sf["relation"]) if ttype == "many2one" and sf.get("relation") else None,
                    selection_values=sel_keys,
                    tracked_transitions=tr_cnt,
                )
                priority = (
                    (10.0 if fname in ("id", "name") else 0.0)
                    + (8.0 if tr_cnt > 0 else 0.0)
                    + (6.0 if role in ("status_enum", "category_enum", "foreign_key", "measure", "limit_or_policy_attr") else 0.0)
                    + min(ent, 4.0)
                    + non_null_rate * 2.0
                )
                field_signals.append((priority, sig))

            all_field_signals_by_model[mname] = field_signals

        # Pre-truncation discovery: hidden string joins & polymorphic discriminators across ALL surviving fields
        id_value_sets: dict[tuple[str, str], set[str]] = {}
        for mname in selected_models:
            rows = sampled_rows_by_model.get(mname, [])
            for _prio, f in all_field_signals_by_model[mname]:
                if f.role == "identifier" and f.ttype == "char" and f.distinct_count >= 2:
                    vals = {
                        str(v)
                        for v in (_normalize_cell(r.get(f.name), "char") for r in rows)
                        if v is not None and len(str(v)) >= 3
                    }
                    if vals:
                        id_value_sets[(mname, f.name)] = vals

        inferred_relations: list[DiscoveredRelation] = []
        boosted_fields: dict[str, set[str]] = defaultdict(set)
        # Candidate target identifier columns across discovered models (e.g. 'name', 'ref')
        target_id_cols = [
            (m, f.name)
            for m in selected_models
            for _prio, f in all_field_signals_by_model[m]
            if f.role == "identifier" and f.ttype == "char" and f.name == "name" and f.distinct_count >= 2
        ]
        for mname in selected_models:
            rows = sampled_rows_by_model.get(mname, [])
            if not rows:
                continue
            for _prio, f in all_field_signals_by_model[mname]:
                if f.ttype != "char" or f.name in ("name", "display_name", "parent_path"):
                    continue
                src_vals = [
                    str(v)
                    for v in (_normalize_cell(r.get(f.name), "char") for r in rows)
                    if v is not None and 3 <= len(str(v)) <= 64
                ]
                if not src_vals:
                    continue
                src_set = sorted(set(src_vals))
                if len(src_set) < 2:
                    continue
                probe_vals = src_set[:25]
                for tgt_model, tgt_field in target_id_cols:
                    if tgt_model == mname:
                        continue
                    # Fast in-sample check first; if partial overlap or looks like cross-doc ref, verify against full table
                    tgt_sample_set = id_value_sets.get((tgt_model, tgt_field), set())
                    in_sample_overlap = len(set(probe_vals) & tgt_sample_set)
                    matched_cnt = 0
                    if in_sample_overlap >= 1 or any(k in f.name.lower() for k in ("origin", "ref", "source", "order", "code")):
                        try:
                            matched_cnt = self.client.search_count(
                                tgt_model, [(tgt_field, "in", probe_vals)]
                            )
                        except Exception:
                            matched_cnt = in_sample_overlap
                    containment = min(1.0, matched_cnt / len(probe_vals))
                    if matched_cnt >= 2 and containment >= 0.4:
                        inferred_relations.append(
                            DiscoveredRelation(
                                source_model=mname,
                                source_field=f.name,
                                target_model=tgt_model,
                                target_field=tgt_field,
                                kind="inferred_string_join",
                                confidence=round(containment, 4),
                                null_rate_in_sample=round(1.0 - f.non_null_rate, 4),
                            )
                        )
                        boosted_fields[mname].add(f.name)

        polymorphics: list[PolymorphicDiscriminator] = []
        for mname in selected_models:
            rows = sampled_rows_by_model.get(mname, [])
            if len(rows) < 6:
                continue
            all_sigs = [sig for _, sig in all_field_signals_by_model[mname]]
            other_fields = [f for f in all_sigs if f.name != "id"]
            for f in all_sigs:
                if f.role != "category_enum" or f.distinct_count < 2:
                    continue
                groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for r in rows:
                    v = _normalize_cell(r.get(f.name), "selection")
                    if v is not None:
                        groups[str(v)].append(r)
                if len(groups) < 2:
                    continue
                divergent: list[str] = []
                max_div = 0.0
                for of in other_fields:
                    if of.name == f.name:
                        continue
                    rates = []
                    for g_rows in groups.values():
                        nn = sum(
                            1
                            for r in g_rows
                            if _normalize_cell(r.get(of.name), of.ttype) is not None
                        )
                        rates.append(nn / len(g_rows))
                    spread = max(rates) - min(rates)
                    if spread >= 0.35:
                        divergent.append(f"{of.name} (nullity spread {spread:.0%})")
                        if spread > max_div:
                            max_div = spread
                if divergent:
                    polymorphics.append(
                        PolymorphicDiscriminator(
                            model=mname,
                            field_name=f.name,
                            value_counts={k: len(v) for k, v in sorted(groups.items())},
                            nullity_divergence_score=round(max_div, 4),
                            divergent_columns=tuple(divergent[:5]),
                        )
                    )
                    boosted_fields[mname].add(f.name)

        # Finalize top-k high-signal fields per model (with boosted join & discriminator fields)
        discovered_models: list[DiscoveredModel] = []
        for mname in selected_models:
            ranked = [
                (prio + (9.0 if sig.name in boosted_fields[mname] else 0.0), sig)
                for prio, sig in all_field_signals_by_model[mname]
            ]
            ranked.sort(key=lambda x: (-x[0], x[1].name))
            retained = tuple(sig for _, sig in ranked[: self.max_fields_per_model])
            total_stored = stored_count_by_model[mname]

            # Archetype inference: ProcessDocument requires tracked lifecycle status (state/status)
            has_lifecycle_status = any(
                f.role == "status_enum"
                and f.tracked_transitions > 0
                and ("state" in f.name.lower() or "status" in f.name.lower())
                for f in retained
            )
            if has_lifecycle_status:
                archetype = "ProcessDocument"
            elif inbound_refs.get(mname, 0) >= 2:
                archetype = "MasterEntity"
            elif any(f.role == "measure" for f in retained) and row_counts.get(mname, 0) > 100:
                archetype = "EventVoucher"
            else:
                archetype = "MasterEntity" if inbound_refs.get(mname, 0) >= 1 else "ObjectType"

            discovered_models.append(
                DiscoveredModel(
                    model=mname,
                    label=model_info[mname]["name"],
                    row_count=row_counts.get(mname, 0),
                    message_count=msg_counts.get(mname, 0),
                    tracked_transition_count=tracked_count_by_model.get(mname, 0),
                    suggested_archetype=archetype,
                    total_stored_fields=total_stored,
                    retained_fields=retained,
                    pruned_field_count=max(0, total_stored - len(retained)),
                    rls_rule_count=rls_counts.get(mname, 0),
                )
            )

        # -------------------------------------------------------------------
        # Stage 3: Autonomous Process Mining (mail.tracking.value -> State Machines)
        # -------------------------------------------------------------------
        mined_sms: list[MinedStateMachine] = []
        tracked_sel_fids = [
            fid
            for fid, fm in tracked_field_meta.items()
            if fm["model"] in selected_set and fm["ttype"] == "selection" and fm["count"] > 0
        ]
        if tracked_sel_fids:
            try:
                trans_groups = self.client.read_group(
                    "mail.tracking.value",
                    [("field_id", "in", tracked_sel_fids)],
                    ["field_id", "old_value_char", "new_value_char"],
                    ["field_id", "old_value_char", "new_value_char"],
                    lazy=False,
                )
            except Exception:
                trans_groups = []

            edges_by_fid: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
            for tg in trans_groups:
                fid_raw = tg.get("field_id")
                if not (isinstance(fid_raw, (list, tuple)) and fid_raw):
                    continue
                fid = int(fid_raw[0])
                old_lbl = str(tg.get("old_value_char") or "").strip()
                new_lbl = str(tg.get("new_value_char") or "").strip()
                cnt = int(tg.get("__count", 0))
                if old_lbl and new_lbl and old_lbl != new_lbl and cnt > 0:
                    edges_by_fid[fid].append((old_lbl, new_lbl, cnt))

            for fid, raw_edges in sorted(edges_by_fid.items()):
                fm = tracked_field_meta[fid]
                mname = str(fm["model"])
                fname = str(fm["name"])
                flabel = str(fm.get("field_description") or fname)
                sel_pairs = selection_maps_by_model.get(mname, {}).get(fname, [])
                label_to_key = {lbl: key for key, lbl in sel_pairs}

                edges: list[MinedTransitionEdge] = []
                out_counts: Counter[str] = Counter()
                in_counts: Counter[str] = Counter()
                pair_counts: dict[tuple[str, str], int] = {}
                for old_lbl, new_lbl, cnt in sorted(raw_edges, key=lambda x: (-x[2], x[0], x[1])):
                    fk = label_to_key.get(old_lbl, old_lbl)
                    tk = label_to_key.get(new_lbl, new_lbl)
                    edges.append(
                        MinedTransitionEdge(
                            from_state=fk,
                            to_state=tk,
                            from_label=old_lbl,
                            to_label=new_lbl,
                            count=cnt,
                        )
                    )
                    out_counts[fk] += cnt
                    in_counts[tk] += cnt
                    pair_counts[(fk, tk)] = cnt

                all_states = set(out_counts) | set(in_counts)
                initials = sorted(s for s in all_states if in_counts[s] == 0)
                if not initials and out_counts:
                    # Cycle exists (e.g. cancelled -> draft -> sale): pick highest net-outflow state
                    initials = [max(all_states, key=lambda s: out_counts[s] - in_counts[s])]
                terminals = sorted(s for s in all_states if out_counts[s] == 0 and in_counts[s] > 0)

                reversals: list[str] = []
                for (fk, tk), cnt in pair_counts.items():
                    if (tk, fk) in pair_counts and pair_counts[(tk, fk)] >= cnt:
                        reversals.append(f"{fk} -> {tk} ({cnt}x, reverses {tk} -> {fk})")
                    elif any(rk in tk.lower() for rk in _REVERSAL_KEYWORDS) and not any(
                        rk in fk.lower() for rk in _REVERSAL_KEYWORDS
                    ):
                        reversals.append(f"{fk} -> {tk} ({cnt}x)")

                mined_sms.append(
                    MinedStateMachine(
                        model=mname,
                        field_name=fname,
                        field_label=flabel,
                        epistemic_tier="INFERRED_TRACE",
                        total_events=sum(e.count for e in edges),
                        edges=tuple(edges),
                        inferred_initial_states=tuple(initials),
                        inferred_terminal_states=tuple(terminals),
                        backward_or_reversal_edges=tuple(sorted(set(reversals))),
                    )
                )

        # -------------------------------------------------------------------
        # Stage 4: Systemic Constraints, Hidden String Joins & Polymorphic Tables
        # -------------------------------------------------------------------
        constraints: list[SystemicConstraint] = []
        if selected_models:
            model_id_to_name = {model_info[m]["id"]: m for m in selected_models if m in model_info}
            try:
                crows = self.client.search_read(
                    "ir.model.constraint",
                    [("model", "in", list(model_id_to_name.keys()))],
                    ["name", "type", "definition", "message", "model"],
                    limit=500,
                )
                for cr in crows:
                    ctype = str(cr.get("type") or "")
                    defn = str(cr.get("definition") or "")
                    # Keep true constraints (unique / check / UNIQUE INDEX), drop plain lookup indexes
                    if ctype not in ("u", "c") and "UNIQUE" not in defn.upper():
                        continue
                    mid = cr.get("model")
                    if not (isinstance(mid, (list, tuple)) and mid and int(mid[0]) in model_id_to_name):
                        continue
                    constraints.append(
                        SystemicConstraint(
                            model=model_id_to_name[int(mid[0])],
                            name=str(cr["name"]),
                            constraint_type=ctype or "unique_index",
                            definition=defn,
                            message=str(cr.get("message") or ""),
                        )
                    )
            except Exception:
                pass

        all_relations = [*explicit_relations, *inferred_relations]

        # -------------------------------------------------------------------
        # Stage 5: Autonomous Anomaly & Epistemic Gray-Zone Mining
        # -------------------------------------------------------------------
        findings: list[EpistemicFinding] = []
        f_idx = 0

        def _add_finding(
            *,
            tier: str,
            kind: str,
            model: str,
            summary: str,
            evidence: dict[str, Any],
            conservative_default: str,
        ) -> None:
            nonlocal f_idx
            f_idx += 1
            findings.append(
                EpistemicFinding(
                    finding_id=f"FDE-{f_idx:03d}",
                    epistemic_tier=tier,
                    kind=kind,
                    model=model,
                    summary=summary,
                    evidence=evidence,
                    conservative_default=conservative_default,
                )
            )

        # 5a. Polymorphic mixed tables -> must filter before grounding
        for p in polymorphics:
            _add_finding(
                tier="INFERRED_TRACE",
                kind="POLYMORPHIC_MIXED_TABLE",
                model=p.model,
                summary=(
                    f"{p.model} mixes {len(p.value_counts)} distinct record subtypes via "
                    f"{p.field_name}={p.value_counts}; schema nullity diverges by "
                    f"{p.nullity_divergence_score:.0%}."
                ),
                evidence={
                    "discriminator_field": p.field_name,
                    "value_counts": p.value_counts,
                    "divergent_columns": list(p.divergent_columns),
                },
                conservative_default=f"SPLIT_OR_FILTER_BY({p.field_name})",
            )

        # 5b. Partial / orphan foreign-key subsets (e.g. rows with FK=False mixed into a linked table)
        dm_by_name = {dm.model: dm for dm in discovered_models}
        for rel in explicit_relations:
            tgt_dm = dm_by_name.get(rel.target_model)
            if tgt_dm is None or tgt_dm.suggested_archetype != "ProcessDocument":
                continue
            if 0.05 <= rel.null_rate_in_sample <= 0.95:
                rows = sampled_rows_by_model.get(rel.source_model, [])
                orphan_ids = [
                    r.get("name") or r.get("id")
                    for r in rows
                    if _normalize_cell(r.get(rel.source_field), "many2one") is None
                ][:5]
                _add_finding(
                    tier="INFERRED_TRACE",
                    kind="ORPHAN_OR_UNLINKED_SUBSET",
                    model=rel.source_model,
                    summary=(
                        f"{rel.source_model}.{rel.source_field} -> {rel.target_model} is empty on "
                        f"{rel.null_rate_in_sample:.0%} of sampled rows (likely out-of-scope internal records)."
                    ),
                    evidence={
                        "field": rel.source_field,
                        "target_model": rel.target_model,
                        "null_rate": rel.null_rate_in_sample,
                        "sample_orphan_records": orphan_ids,
                    },
                    conservative_default=f"FILTER_NON_NULL({rel.source_field})",
                )

        # 5c. Cross-table numeric limit exceedance contradiction (purely generic, deduped per model pair!)
        rows_by_model_and_id: dict[str, dict[Any, dict[str, Any]]] = {
            m: {r.get("id"): r for r in rows if r.get("id") is not None}
            for m, rows in sampled_rows_by_model.items()
        }
        seen_limit_pairs: set[tuple[str, str, str]] = set()
        # Sort explicit_relations shortest source_field first so canonical FK (e.g. partner_id) wins over partner_invoice_id
        for rel in sorted(explicit_relations, key=lambda r: (len(r.source_field), r.source_field)):
            src_dm = dm_by_name.get(rel.source_model)
            tgt_dm = dm_by_name.get(rel.target_model)
            if src_dm is None or tgt_dm is None:
                continue
            src_measures = [f for f in src_dm.retained_fields if f.role == "measure"]
            tgt_limits = [f for f in tgt_dm.retained_fields if f.role == "limit_or_policy_attr"]
            status_field = next((f.name for f in src_dm.retained_fields if f.role == "status_enum"), None)
            if not src_measures or not tgt_limits:
                continue
            src_rows = sampled_rows_by_model.get(rel.source_model, [])
            tgt_lookup = rows_by_model_and_id.get(rel.target_model, {})

            missing_parent_ids = sorted(
                {
                    int(pid)
                    for r in src_rows
                    if (pid := _normalize_cell(r.get(rel.source_field), "many2one")) is not None
                    and pid not in tgt_lookup
                }
            )
            if missing_parent_ids:
                try:
                    lim_fnames = ["id", "name", *[f.name for f in tgt_limits]]
                    fetched = self.client.search_read(
                        rel.target_model,
                        [("id", "in", missing_parent_ids[:50])],
                        lim_fnames,
                        limit=50,
                    )
                    for fr in fetched:
                        tgt_lookup[fr["id"]] = fr
                except Exception:
                    pass

            for lf in tgt_limits[:2]:
                dedup_key = (rel.source_model, rel.target_model, lf.name)
                if dedup_key in seen_limit_pairs:
                    continue
                mf = src_measures[0]
                exceeded_by_status: Counter[str] = Counter()
                within_by_status: Counter[str] = Counter()
                exceeded_samples: list[dict[str, Any]] = []
                for r in src_rows:
                    pid = _normalize_cell(r.get(rel.source_field), "many2one")
                    parent = tgt_lookup.get(pid)
                    if parent is None:
                        continue
                    mv = _normalize_cell(r.get(mf.name), mf.ttype)
                    lv = _normalize_cell(parent.get(lf.name), lf.ttype)
                    if not isinstance(mv, (int, float)) or not isinstance(lv, (int, float)):
                        continue
                    if lv <= 0 or mv <= 0:
                        continue
                    st = str(r.get(status_field) or "unknown") if status_field else "unknown"
                    if mv > lv:
                        exceeded_by_status[st] += 1
                        if len(exceeded_samples) < 5:
                            exceeded_samples.append(
                                {
                                    "record": r.get("name") or r.get("id"),
                                    mf.name: mv,
                                    f"parent.{lf.name}": lv,
                                    "status": st,
                                }
                            )
                    else:
                        within_by_status[st] += 1
                total_exc = sum(exceeded_by_status.values())
                if total_exc >= 1:
                    seen_limit_pairs.add(dedup_key)
                    _add_finding(
                        tier="AMBIGUOUS_GRAY_ZONE",
                        kind="CROSS_ENTITY_LIMIT_EXCEEDANCE_CONTRADICTION",
                        model=rel.source_model,
                        summary=(
                            f"Found {total_exc} {rel.source_model} record(s) where "
                            f"{mf.name} > {rel.target_model}.{lf.name}, spanning statuses "
                            f"{dict(exceeded_by_status)} (normative policy undecided without human/doc)."
                        ),
                        evidence={
                            "measure_field": mf.name,
                            "parent_relation": rel.source_field,
                            "parent_limit_field": f"{rel.target_model}.{lf.name}",
                            "exceeded_status_counts": dict(exceeded_by_status),
                            "within_status_counts": dict(within_by_status),
                            "sample_records": exceeded_samples,
                        },
                        conservative_default="PENDING_APPROVAL_WHEN_EXCEEDED",
                    )

        # 5d. Backward/cancellation reversal loops in mined state machines
        for sm in mined_sms:
            if sm.backward_or_reversal_edges:
                _add_finding(
                    tier="AMBIGUOUS_GRAY_ZONE",
                    kind="STATE_REVERSAL_OR_CANCELLATION_LOOP",
                    model=sm.model,
                    summary=(
                        f"{sm.model}.{sm.field_name} exhibits historical reversal/cancellation edges: "
                        + "; ".join(sm.backward_or_reversal_edges)
                    ),
                    evidence={
                        "field": sm.field_name,
                        "reversal_edges": list(sm.backward_or_reversal_edges),
                        "total_events": sm.total_events,
                    },
                    conservative_default="REQUIRE_APPROVAL_OR_AUDIT_ON_REVERSAL",
                )

        retained_field_total = sum(len(m.retained_fields) for m in discovered_models)
        comp_ratio = (
            round(retained_field_total / total_fields_scanned, 5)
            if total_fields_scanned > 0
            else 0.0
        )

        return FDEScoutReport(
            source_locator=self.source_locator,
            task_intent=task_intent,
            total_models_scanned=total_models_scanned,
            total_fields_scanned=total_fields_scanned,
            total_tracking_rows=total_tracking_rows,
            retained_model_count=len(discovered_models),
            retained_field_count=retained_field_total,
            field_compression_ratio=comp_ratio,
            discovered_models=tuple(discovered_models),
            discovered_relations=tuple(all_relations),
            mined_state_machines=tuple(mined_sms),
            systemic_constraints=tuple(constraints),
            polymorphic_discriminators=tuple(polymorphics),
            epistemic_findings=tuple(findings),
            sampled_rows_by_model={
                m: tuple(dict(r) for r in sampled_rows_by_model.get(m, ()))
                for m in selected_models
            },
        )


def format_scout_summary(report: FDEScoutReport) -> str:
    lines = [
        f"=== Autonomous FDE Scout Report ({report.source_locator}) ===",
        f"Task intent: {report.task_intent or '(unsupervised / pure activity discovery)'}",
        (
            f"Compression: {report.total_models_scanned} models -> {report.retained_model_count} models; "
            f"{report.total_fields_scanned} fields -> {report.retained_field_count} high-entropy fields "
            f"({report.field_compression_ratio:.2%} retained); {report.total_tracking_rows} tracking events scanned"
        ),
        "",
        "[1] Discovered Active Business Models:",
    ]
    for m in report.discovered_models:
        f_preview = ", ".join(f"{f.name}({f.role})" for f in m.retained_fields[:6])
        lines.append(
            f"  - {m.model} ({m.label}) [{m.suggested_archetype}] | rows={m.row_count}, "
            f"msgs={m.message_count}, tracked={m.tracked_transition_count}, rls={m.rls_rule_count} | "
            f"fields {m.total_stored_fields}->{len(m.retained_fields)}: {f_preview}"
        )
    lines.append("\n[2] Discovered Relations (Explicit FK + Hidden String Joins):")
    for r in report.discovered_relations:
        lines.append(
            f"  - {r.source_model}.{r.source_field} -> {r.target_model}.{r.target_field} "
            f"[{r.kind}, conf={r.confidence:.2f}, null={r.null_rate_in_sample:.0%}]"
        )
    lines.append("\n[3] Mined State Machines (INFERRED_TRACE from mail.tracking.value):")
    for sm in report.mined_state_machines:
        edge_str = ", ".join(f"{e.from_state}->{e.to_state}({e.count})" for e in sm.edges[:6])
        lines.append(
            f"  - {sm.model}.{sm.field_name} ({sm.total_events} events) | init={list(sm.inferred_initial_states)} "
            f"| term={list(sm.inferred_terminal_states)} | {edge_str}"
        )
    lines.append(f"\n[4] Systemic Constraints (PROVEN_SYSTEMIC): {len(report.systemic_constraints)} constraints")
    for c in report.systemic_constraints[:8]:
        lines.append(f"  - [{c.model}] {c.name}: {c.definition}")
    lines.append(f"\n[5] Epistemic Findings & Gray Zones ({len(report.epistemic_findings)}):")
    for ef in report.epistemic_findings:
        lines.append(
            f"  - {ef.finding_id} [{ef.epistemic_tier}] {ef.kind} ({ef.model}): {ef.summary} "
            f"==> default: {ef.conservative_default}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Autonomous Zero-Hardcoding Read-Only Odoo FDE Scout")
    parser.add_argument("--db", default=DB_NORTH, help=f"Odoo database name (default: {DB_NORTH})")
    parser.add_argument("--intent", default="", help="Optional natural-language task intent (leave empty for pure unsupervised activity discovery)")
    parser.add_argument("--max-models", type=int, default=6, help="Max models in discovered active subgraph")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit structured JSON report")
    args = parser.parse_args(argv)

    client = ReadOnlyOdooClient(db=args.db)
    scout = OdooFDEScout(
        client,
        source_locator=f"odoo://{args.db}",
        max_models=args.max_models,
    )
    report = scout.scout(task_intent=args.intent)
    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_scout_summary(report))


if __name__ == "__main__":
    main()
