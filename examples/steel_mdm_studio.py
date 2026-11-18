"""投资人路演演播室 #2：钢厂主数据（MDM）两大死结可视化交互演播台。

架构严格零改表破局：
- W_real (`legacy_erp.json`)：老 ERP 源表仅含 Manufacturer 与混乱 ContractSKU（无规格列、无 MaterialSpec/MaterialLot）；
- W_mem (`w_mem.sqlite3`)：OntologyRuntime 记忆棋盘 + `ontology_overlay` 属性级寄生层 + 三正交边高维批次桶；
- W_lat (`w_lat.sqlite3`)：持久 LatentSpace 增量建码双硬护栏（UNMAPPED_PHYSICAL_SPEC / SPEC_QUALITY_MISMATCH）
  与新物理品种审批隔离队列；
- 三视角恒等账本：reconcile_multi_view_ledger + ObjectSet/pivot/aggregate + 1 秒时光机无损冲正。

严禁修改 open_ontology/ 与 examples/steel_mdm_demo.py，仅做兼容只读导入与驱动。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from open_ontology import (
    Actor,
    GovernanceViolationError,
    InvariantError,
    LatentSpace,
    OntologyRuntime,
)

from . import steel_mdm_demo as mdm

_SYSTEM_ACTOR = Actor(actor_id="__mdm_studio__", role="system", clearance_tags={"*"})

_LEGACY_SKU_SEEDS: list[dict[str, Any]] = [
    {
        "sku_id": "ERP-SKU-9001",
        "invoice_title": "山西焦化11月特供高强焦(含税专票)",
        "brand_id": "BRAND-1",
        "unit_price_ton": 2000,
        "ash_pct": 12.0,
        "sulfur_pct": 0.6,
    },
    {
        "sku_id": "ERP-SKU-9002",
        "invoice_title": "山西焦化冬季合同焦A批",
        "brand_id": "BRAND-1",
        "unit_price_ton": 2050,
        "ash_pct": 11.8,
        "sulfur_pct": 0.5,
    },
    {
        "sku_id": "ERP-SKU-9003",
        "invoice_title": "山西焦化二级焦散码",
        "brand_id": "BRAND-1",
        "unit_price_ton": 1600,
        "ash_pct": 13.2,
        "sulfur_pct": 0.8,
    },
    {
        "sku_id": "ERP-SKU-9004",
        "invoice_title": "河北焦化一级焦合同码",
        "brand_id": "BRAND-2",
        "unit_price_ton": 1950,
        "ash_pct": 12.2,
        "sulfur_pct": 0.6,
    },
]

_LEGACY_MANUFACTURERS: list[dict[str, Any]] = [
    {"brand_id": "BRAND-1", "name": "山西焦化"},
    {"brand_id": "BRAND-2", "name": "河北焦化"},
]

_DEFAULT_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("ERP-SKU-9001", "SPEC-A"),
    ("ERP-SKU-9002", "SPEC-A"),
    ("ERP-SKU-9003", "SPEC-B"),
    ("ERP-SKU-9004", "SPEC-A"),
)

_DEFAULT_LOTS: tuple[tuple[str, str, int], ...] = (
    ("LOT-01", "ERP-SKU-9001", 100),
    ("LOT-02", "ERP-SKU-9003", 80),
    ("LOT-03", "ERP-SKU-9004", 120),
)

_ACTION_CN = {
    "map_legacy_sku_to_spec": "外挂贴片归并",
    "register_contract_sku": "增量建码护栏",
    "receive_material_lot": "高维批次桶入库",
    "consume_by_brand_fifo": "车间按厂家盲投先进先出",
}

_BRAND_CN = {
    "BRAND-1": "山西焦化厂",
    "BRAND-2": "河北煤化厂",
}

_SPEC_CN = {
    "SPEC-A": "一级冶金焦",
    "SPEC-B": "二级冶金焦",
    "SPEC-C": "三级特种焦",
}

_SPEC_FULL_CN = {
    "SPEC-A": "一级冶金焦（热态强度 ≥ 65%）",
    "SPEC-B": "二级冶金焦（热态强度 ≥ 55%）",
    "SPEC-C": "三级特种焦（热态强度 ≥ 45%）",
}

_LOT_CN = {
    "LOT-01": "批次 101",
    "LOT-02": "批次 102",
    "LOT-03": "批次 103",
}

_ERROR_CN = {
    "UNMAPPED_PHYSICAL_SPEC": "未绑定标准规格",
    "SPEC_QUALITY_MISMATCH": "煤质指标以次充好",
    "INSUFFICIENT_BRAND_STOCK": "厂家可用库存不足",
    "INVALID_UNIT_PRICE": "含税单价无效",
    "BRAND_NOT_FOUND": "供货厂家不存在",
}


def _strip_latin_tokens(text: Any) -> str:
    raw = str(text or "")
    cleaned = re.sub(r"[A-Za-z_]+", "", raw)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _cn_sku_id(sku_id: Any) -> str:
    s = str(sku_id or "").strip()
    if not s:
        return "未知物料码"
    m = re.search(r"(\d+)$", s)
    if m:
        return f"散码 {m.group(1)}"
    return _strip_latin_tokens(s) or "采购散码"


def _cn_invoice_title(title: Any) -> str:
    s = str(title or "").strip()
    s = s.replace("冬季合同焦A批", "冬季合同焦甲批")
    s = s.replace("SPEC-A", "一级冶金焦").replace("SPEC-B", "二级冶金焦").replace("SPEC-C", "三级特种焦")
    s = re.sub(r"\(CSR>=(\d+)%\)", r"（热态强度 ≥ \1%）", s)
    return _strip_latin_tokens(s) or "采购合同煤焦"


def _cn_brand(brand_id: Any) -> str:
    key = str(brand_id or "").strip()
    if key in _BRAND_CN:
        return _BRAND_CN[key]
    return _strip_latin_tokens(key) or "未知供货厂家"


def _cn_spec(spec_id: Any, *, full: bool = False) -> str:
    if spec_id is None or spec_id == "":
        return "尚未归并"
    key = str(spec_id).strip()
    mapping = _SPEC_FULL_CN if full else _SPEC_CN
    if key in mapping:
        return mapping[key]
    return _strip_latin_tokens(key) or "自定义物理规格"


def _cn_lot(lot_id: Any) -> str:
    key = str(lot_id or "").strip()
    if key in _LOT_CN:
        return _LOT_CN[key]
    m = re.search(r"(\d+)$", key)
    return f"批次 {m.group(1)}" if m else (_strip_latin_tokens(key) or "原料批次")


def _cn_actor(actor_id: Any) -> str:
    s = str(actor_id or "").strip()
    if not s:
        return "—"
    if "steward" in s:
        return "主数据管家"
    if "buyer" in s:
        return "采购专员"
    if "mes" in s:
        return "车间操作员"
    return _strip_latin_tokens(s) or "业务专员"


def _cn_run_label(run_id: Any) -> str:
    nums = re.findall(r"\d+", str(run_id or ""))
    return f"流水 {nums[-1]} 号" if nums else "审计流水"


def _cn_error_msg(code: str, raw_msg: str) -> str:
    if code == "UNMAPPED_PHYSICAL_SPEC":
        return "采购新建物料码必须绑定标准物理规格，或申请新增物理品种签批"
    if code == "SPEC_QUALITY_MISMATCH":
        return "化验灰分或硫分超出高品位规格上限，已按煤质红线拦截（谨防以次充好）"
    if code == "INSUFFICIENT_BRAND_STOCK":
        return "该供货厂家当前在库批次总吨数不足，无法超额领料"
    if code == "INVALID_UNIT_PRICE":
        return "含税单价必须大于 ¥0"
    if code == "BRAND_NOT_FOUND":
        return "所填供货厂家尚未登记建档，请核对后重试"
    return _strip_latin_tokens(raw_msg) or "已按规则拦截"


def _normalize_sku_input(val: Any) -> str:
    raw = str(val or "").strip()
    if not raw:
        return ""
    if re.match(r"^[A-Za-z0-9\-_]+$", raw):
        if raw.isdigit():
            return f"ERP-SKU-{raw}"
        return raw
    m = re.search(r"(\d+)", raw)
    return f"ERP-SKU-{m.group(1)}" if m else raw


def _normalize_brand_input(val: Any) -> str:
    raw = str(val or "").strip()
    if raw in ("山西焦化厂", "山西焦化", "BRAND-1"):
        return "BRAND-1"
    if raw in ("河北煤化厂", "河北焦化", "BRAND-2"):
        return "BRAND-2"
    return raw


def _normalize_spec_input(val: Any) -> str | None:
    if val is None:
        return None
    raw = str(val).strip()
    if not raw or raw in ("无", "留空"):
        return None
    if raw in ("一级冶金焦", "SPEC-A"):
        return "SPEC-A"
    if raw in ("二级冶金焦", "SPEC-B"):
        return "SPEC-B"
    if raw in ("三级特种焦", "三级冶金焦", "SPEC-C"):
        return "SPEC-C"
    return raw


def _normalize_new_spec_pair(req_id: Any, req_name: Any) -> tuple[str | None, str | None]:
    if req_id is None or str(req_id).strip() == "":
        return None, (str(req_name).strip() if req_name else None)
    raw_id = str(req_id).strip()
    raw_name = str(req_name).strip() if req_name else None
    if raw_id in ("三级特种焦", "三级冶金焦", "SPEC-C"):
        return "SPEC-C", raw_name or "三级特种焦（热态强度 ≥ 45%）"
    if raw_id in ("一级冶金焦", "SPEC-A"):
        return "SPEC-A", raw_name or "一级冶金焦（热态强度 ≥ 65%）"
    if raw_id in ("二级冶金焦", "SPEC-B"):
        return "SPEC-B", raw_name or "二级冶金焦（热态强度 ≥ 55%）"
    if re.search(r"[\u4e00-\u9fff]", raw_id) and not re.match(r"^[A-Za-z0-9\-_]+$", raw_id):
        return "SPEC-CUSTOM", raw_name or raw_id
    return raw_id, raw_name


class SteelMDMStudioApp:
    """钢厂 MDM 两大死结交互演播台（W_real · W_mem · W_lat + 三视角对账）。"""

    def __init__(
        self,
        state_dir: str | Path = "/tmp/open_ontology_steel_mdm_studio",
        *,
        port: int = 8792,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.port = int(port)

        self.legacy_erp_path = self.state_dir / "legacy_erp.json"
        self.mem_db_path = self.state_dir / "w_mem.sqlite3"
        self.lat_db_path = self.state_dir / "w_lat.sqlite3"
        self.meta_path = self.state_dir / "studio_meta.json"

        self._lock = threading.RLock()
        self.memory_runtime: OntologyRuntime | None = None
        self.latent: LatentSpace | None = None
        self.legacy_erp: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}

        self._init_or_rehydrate()

    # -- Store & Meta Persistence -----------------------------------------
    def _close_stores(self) -> None:
        if self.latent is not None:
            self.latent.close()
            self.latent = None
        if self.memory_runtime is not None:
            self.memory_runtime.close()
            self.memory_runtime = None

    def close(self) -> None:
        with self._lock:
            self._close_stores()

    def _default_legacy_erp(self) -> dict[str, Any]:
        return {
            "schema_note": "老 ERP 物理源表无 canonical_spec_id 列、无 MaterialSpec/MaterialLot 表",
            "reindex_count": 0,
            "last_reindex_at": None,
            "Manufacturer": copy.deepcopy(_LEGACY_MANUFACTURERS),
            "ContractSKU": copy.deepcopy(_LEGACY_SKU_SEEDS),
        }

    def _default_meta(self) -> dict[str, Any]:
        return {
            "seq": 0,
            "completed_acts": [],
            "acts": {},
            "last_probe": None,
            "timeline": [],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _save_legacy_erp(self) -> None:
        tmp = self.legacy_erp_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.legacy_erp, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.legacy_erp_path)

    def _save_meta(self) -> None:
        self.meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.meta_path)

    def _next_run_id(self, prefix: str) -> str:
        self.meta["seq"] = int(self.meta.get("seq", 0)) + 1
        return f"{prefix}-{self.meta['seq']:04d}"

    def _push_timeline(self, stage: str, title: str, detail: str, tone: str = "info") -> None:
        items = list(self.meta.get("timeline") or [])
        items.append(
            {
                "at": time.strftime("%H:%M:%S"),
                "stage": stage,
                "title": title,
                "detail": detail,
                "tone": tone,
            }
        )
        self.meta["timeline"] = items[-24:]

    def _mark_act_done(self, act_key: str, payload: dict[str, Any]) -> None:
        completed = list(self.meta.get("completed_acts") or [])
        if act_key not in completed:
            completed.append(act_key)
        self.meta["completed_acts"] = completed
        acts = dict(self.meta.get("acts") or {})
        acts[act_key] = payload
        self.meta["acts"] = acts

    def _open_stores(self) -> None:
        self.memory_runtime = OntologyRuntime(mdm.spec, db_path=self.mem_db_path)
        self.latent = self.memory_runtime.open_latent_space(self.lat_db_path)

    def _init_or_rehydrate(self) -> None:
        with self._lock:
            if (
                self.legacy_erp_path.is_file()
                and self.mem_db_path.is_file()
                and self.lat_db_path.is_file()
                and self.meta_path.is_file()
            ):
                try:
                    self.legacy_erp = json.loads(self.legacy_erp_path.read_text(encoding="utf-8"))
                    self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                    self._open_stores()
                    self._save_meta()
                    return
                except Exception:
                    self._close_stores()

            self.reset()

    def reset(self) -> dict[str, Any]:
        """重置演播台：还原老 ERP 4 个未归并脏散码与基础规格库。"""
        with self._lock:
            self._close_stores()
            for db_file in (self.mem_db_path, self.lat_db_path):
                if db_file.exists():
                    db_file.unlink()
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{db_file}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()

            self.legacy_erp = self._default_legacy_erp()
            self.meta = self._default_meta()
            self._save_legacy_erp()

            self.memory_runtime = OntologyRuntime(mdm.spec, db_path=self.mem_db_path)
            mdm._seed_specs(self.memory_runtime)
            self.memory_runtime.load_snapshot(
                {
                    "Manufacturer": copy.deepcopy(self.legacy_erp["Manufacturer"]),
                    "ContractSKU": copy.deepcopy(self.legacy_erp["ContractSKU"]),
                }
            )
            self.latent = self.memory_runtime.open_latent_space(self.lat_db_path)

            self._push_timeline(
                "INIT",
                "老 ERP 存量初始化",
                "已载入 BRAND-1/BRAND-2 与 ERP-SKU-9001..9004 四条未归并散码，准备演绎五大破局场景。",
                "ok",
            )
            self._save_meta()
            return self.state_snapshot()

    # -- Helpers -----------------------------------------------------------
    def _ensure_ready(self) -> tuple[OntologyRuntime, LatentSpace]:
        assert self.memory_runtime is not None and self.latent is not None
        return self.memory_runtime, self.latent

    def _safe_rebase_if_clear(self) -> None:
        rt, latent = self._ensure_ready()
        del rt
        if not latent.pending_moves() and not latent.awaiting_approval_moves():
            latent.rebase()

    def _stage_and_settle_owned(
        self,
        run_id: str,
        action_name: str,
        params: dict[str, Any],
        actor: Actor,
    ) -> dict[str, Any]:
        rt, latent = self._ensure_ready()
        del rt
        # 若此前有未签批的无关暂存（例如 Act 3 挂起期间试用其他只写 Owned 动作），确保不会有 pending 堵塞
        if latent.pending_moves():
            raise GovernanceViolationError("unexpected unsettled pending moves in W_lat")
        latent.stage_move(run_id, action_name, params, actor)
        return latent.settle_move(run_id)

    def _upsert_legacy_sku_source(self, source_fields: dict[str, Any]) -> None:
        skus = list(self.legacy_erp.get("ContractSKU") or [])
        filtered = [row for row in skus if row.get("sku_id") != source_fields["sku_id"]]
        filtered.append(
            {
                "sku_id": str(source_fields["sku_id"]),
                "invoice_title": str(source_fields["invoice_title"]),
                "brand_id": str(source_fields["brand_id"]),
                "unit_price_ton": int(source_fields["unit_price_ton"]),
                "ash_pct": float(source_fields["ash_pct"]),
                "sulfur_pct": float(source_fields["sulfur_pct"]),
            }
        )
        self.legacy_erp["ContractSKU"] = filtered
        self._save_legacy_erp()

    def _remove_legacy_sku_source(self, sku_id: str) -> None:
        skus = list(self.legacy_erp.get("ContractSKU") or [])
        self.legacy_erp["ContractSKU"] = [row for row in skus if row.get("sku_id") != sku_id]
        self._save_legacy_erp()

    def _legacy_snapshot_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "Manufacturer": copy.deepcopy(self.legacy_erp.get("Manufacturer") or []),
            "ContractSKU": copy.deepcopy(self.legacy_erp.get("ContractSKU") or []),
        }

    def _overlay_rows(self) -> list[dict[str, Any]]:
        rt, _ = self._ensure_ready()
        rows = rt.connection.execute(
            "SELECT object_type, object_id, patch_json FROM ontology_overlay "
            "ORDER BY object_type, object_id"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "properties": json.loads(row["patch_json"]),
                }
            )
        return out

    # -- Act 1 & Reindex ---------------------------------------------------
    def reindex_legacy_erp(self) -> dict[str, Any]:
        """模拟老 ERP 夜间全量快照覆写 (load_snapshot)，证明 overlay 与自有主数据 100% 存活。"""
        with self._lock:
            rt, _ = self._ensure_ready()
            before = {
                sku.sku_id: sku.canonical_spec_id
                for sku in rt.query_objects(mdm.ContractSKU, mdm.STEWARD)
            }
            payload = self._legacy_snapshot_payload()
            snap_result = rt.load_snapshot(payload)
            self._safe_rebase_if_clear()

            after = {
                sku.sku_id: sku.canonical_spec_id
                for sku in rt.query_objects(mdm.ContractSKU, mdm.STEWARD)
            }
            links_after = {
                sku.sku_id: [
                    sp.spec_id
                    for sp in rt.get_linked(
                        sku, mdm.SKU_MAPS_TO_SPEC, mdm.MaterialSpec, mdm.STEWARD
                    )
                ]
                for sku in rt.query_objects(mdm.ContractSKU, mdm.STEWARD)
            }
            self.legacy_erp["reindex_count"] = int(self.legacy_erp.get("reindex_count", 0)) + 1
            self.legacy_erp["last_reindex_at"] = time.strftime("%H:%M:%S")
            self._save_legacy_erp()

            overlay_rows = self._overlay_rows()
            preserved = before == after and all(
                after.get(sku_id) == spec_id for sku_id, spec_id in _DEFAULT_MAPPINGS
            )
            res = {
                "ok": True,
                "reindex_count": self.legacy_erp["reindex_count"],
                "snapshot_result": snap_result,
                "mapped_before": before,
                "mapped_after": after,
                "links_after": links_after,
                "overlay_rows_count": len(overlay_rows),
                "overlay_preserved": preserved,
            }
            self._push_timeline(
                "REINDEX",
                f"老 ERP 第 {self.legacy_erp['reindex_count']} 次夜间全量重刷",
                f"load_snapshot 覆写源表字段后，{len(overlay_rows)} 条 ontology_overlay 与关联边 100% 存活。",
                "ok" if preserved else "warn",
            )
            self._save_meta()
            return {"reindex_result": res, "state": self.state_snapshot()}

    def run_act_1(self) -> dict[str, Any]:
        """第一幕：死结一破局 — 4 散码零改表寄生归并 + 3 高维批次桶入库 + 夜间重刷存活。"""
        with self._lock:
            rt, _ = self._ensure_ready()
            mapped_runs: list[dict[str, Any]] = []
            for sku_id, spec_id in _DEFAULT_MAPPINGS:
                sku = rt.get_object(mdm.ContractSKU, sku_id, mdm.STEWARD)
                if sku.canonical_spec_id != spec_id:
                    rid = self._next_run_id(f"MDM-MAP-{sku_id[-4:]}")
                    settled = self._stage_and_settle_owned(
                        rid,
                        "map_legacy_sku_to_spec",
                        {
                            "sku_id": sku_id,
                            "canonical_spec_id": spec_id,
                            "mapped_by": mdm.BUYER.actor_id,
                        },
                        mdm.BUYER,
                    )
                    mapped_runs.append({"run_id": rid, "sku_id": sku_id, "spec_id": spec_id, "status": settled["status"]})

            lot_runs: list[dict[str, Any]] = []
            existing_lots = {lot.lot_id for lot in rt.query_objects(mdm.MaterialLot, mdm.STEWARD)}
            for lot_id, sku_id, tons in _DEFAULT_LOTS:
                if lot_id not in existing_lots:
                    rid = self._next_run_id(f"MDM-RECV-{lot_id}")
                    settled = self._stage_and_settle_owned(
                        rid,
                        "receive_material_lot",
                        {"lot_id": lot_id, "sku_id": sku_id, "initial_tons": tons},
                        mdm.BUYER,
                    )
                    lot_runs.append({"run_id": rid, "lot_id": lot_id, "sku_id": sku_id, "tons": tons, "status": settled["status"]})

            reindex_info = self.reindex_legacy_erp()["reindex_result"]
            act1_summary = {
                "ok": True,
                "act": 1,
                "mapped_runs": mapped_runs,
                "lot_runs": lot_runs,
                "reindex": reindex_info,
            }
            self._mark_act_done("1", act1_summary)
            self._push_timeline(
                "ACT 1",
                "存量散码零改表寄生归并完成",
                "ERP-SKU-9001..9004 仅写 ontology_overlay 归并边，并完成老 ERP 重刷存活验证。",
                "ok",
            )
            self._save_meta()
            return {"act_result": act1_summary, "state": self.state_snapshot()}

    # -- Act 2 & Custom Probe ----------------------------------------------
    def try_register_sku(self, payload: dict[str, Any]) -> dict[str, Any]:
        """在持久隐空间尝试增量建码（支持前端自定义破坏性试探与智能推荐）。"""
        with self._lock:
            rt, latent = self._ensure_ready()
            sku_id = _normalize_sku_input(payload.get("sku_id"))
            invoice_title = str(payload.get("invoice_title") or "").strip()
            brand_id = _normalize_brand_input(payload.get("brand_id"))
            unit_price_ton = int(payload.get("unit_price_ton") or 0)
            ash_pct = float(payload.get("ash_pct") or 0.0)
            sulfur_pct = float(payload.get("sulfur_pct") or 0.0)
            canonical_spec_id = _normalize_spec_input(payload.get("canonical_spec_id"))
            request_new_spec_id, request_new_spec_name = _normalize_new_spec_pair(
                payload.get("request_new_spec_id"),
                payload.get("request_new_spec_name"),
            )

            recommendations = rt.call_function(
                "recommend_spec_for_sku",
                {"ash_pct": ash_pct, "sulfur_pct": sulfur_pct},
                mdm.BUYER,
            )

            params: dict[str, Any] = {
                "sku_id": sku_id,
                "invoice_title": invoice_title,
                "brand_id": brand_id,
                "unit_price_ton": unit_price_ton,
                "ash_pct": ash_pct,
                "sulfur_pct": sulfur_pct,
                "canonical_spec_id": canonical_spec_id,
                "request_new_spec_id": request_new_spec_id,
                "request_new_spec_name": request_new_spec_name,
            }
            run_id = self._next_run_id("MDM-REG")
            try:
                staged = latent.stage_move(run_id, "register_contract_sku", params, mdm.BUYER)
            except InvariantError as exc:
                res = {
                    "ok": True,
                    "blocked": True,
                    "status": "REJECTED",
                    "run_id": run_id,
                    "sku_id": sku_id,
                    "error_code": exc.code or "INVARIANT_VIOLATION",
                    "error_message": str(exc),
                    "recommendations": recommendations,
                }
                self.meta["last_probe"] = res
                hist = list(self.meta.get("guardrail_history") or [])
                hist.append(
                    {
                        "run_id": run_id,
                        "action_type": "register_contract_sku",
                        "sku_id": sku_id,
                        "error_code": res["error_code"],
                        "error_message": res["error_message"],
                    }
                )
                self.meta["guardrail_history"] = hist[-12:]
                self._push_timeline(
                    "护栏拦截",
                    f"拦截违规建码 {sku_id}: {res['error_code']}",
                    f"{res['error_message']}"
                    + (
                        f"｜智能推荐：{[r['spec_id'] for r in recommendations]}"
                        if recommendations
                        else ""
                    ),
                    "warn",
                )
                self._save_meta()
                return {"probe_result": res, "state": self.state_snapshot()}

            if staged["status"] == "PENDING_APPROVAL":
                res = {
                    "ok": True,
                    "blocked": False,
                    "status": "PENDING_APPROVAL",
                    "run_id": run_id,
                    "sku_id": sku_id,
                    "request_new_spec_id": request_new_spec_id,
                    "request_new_spec_name": request_new_spec_name,
                    "recommendations": recommendations,
                }
                self.meta["last_probe"] = res
                self._push_timeline(
                    "W_lat 挂起",
                    f"新物理品种 {request_new_spec_id} ({sku_id}) 已锁入 W_lat 待批",
                    "W_mem 主世界与老 ERP 零污染，等待 MDM 管家签批。",
                    "warn",
                )
                self._save_meta()
                return {"probe_result": res, "state": self.state_snapshot()}

            # 合法增量绑定已有规格 -> 写入模拟老 ERP 源表 -> 读回结算进 W_mem
            self._upsert_legacy_sku_source(params)
            settled = latent.settle_move(
                run_id, readback_objects=self._legacy_snapshot_payload()
            )
            res = {
                "ok": True,
                "blocked": False,
                "status": settled["status"],
                "run_id": run_id,
                "sku_id": sku_id,
                "canonical_spec_id": canonical_spec_id,
                "recommendations": recommendations,
            }
            self.meta["last_probe"] = res
            self._push_timeline(
                "合规建码",
                f"新建散码 {sku_id} 绑定 {canonical_spec_id} 并落盘",
                f"经 W_lat 校验并通过源表回读核验（{settled['status']}）。",
                "ok",
            )
            self._save_meta()
            return {"probe_result": res, "state": self.state_snapshot()}

    def run_act_2(self) -> dict[str, Any]:
        """第二幕：源头增量建码双硬护栏（野码 UNMAPPED_PHYSICAL_SPEC + 以次充好 SPEC_QUALITY_MISMATCH）。"""
        with self._lock:
            probe_wild = self.try_register_sku(
                {
                    "sku_id": "ERP-SKU-9100",
                    "invoice_title": "无归属野码",
                    "brand_id": "BRAND-1",
                    "unit_price_ton": 1900,
                    "ash_pct": 12.0,
                    "sulfur_pct": 0.6,
                }
            )["probe_result"]

            probe_mismatch = self.try_register_sku(
                {
                    "sku_id": "ERP-SKU-9101",
                    "invoice_title": "以次充好挂一级焦",
                    "brand_id": "BRAND-1",
                    "unit_price_ton": 2000,
                    "ash_pct": 13.2,
                    "sulfur_pct": 0.8,
                    "canonical_spec_id": "SPEC-A",
                }
            )["probe_result"]

            act2_summary = {
                "ok": True,
                "act": 2,
                "wild_sku_probe": probe_wild,
                "quality_mismatch_probe": probe_mismatch,
            }
            self._mark_act_done("2", act2_summary)
            self._save_meta()
            return {"act_result": act2_summary, "state": self.state_snapshot()}

    # -- Act 3: New Physical Variety Approval in W_lat ---------------------
    def run_act_3(self) -> dict[str, Any]:
        """第三幕：采购申报全新物理品种 SPEC-C，锁入持久隐空间 W_lat 待 MDM 管家签批。"""
        with self._lock:
            rt, latent = self._ensure_ready()
            awaiting = latent.awaiting_approval_moves()
            existing_req = next(
                (
                    mv
                    for mv in awaiting
                    if (mv.get("params") or {}).get("request_new_spec_id") == "SPEC-C"
                ),
                None,
            )
            if existing_req is None:
                if rt.get_object_or_none(mdm.ContractSKU, "ERP-SKU-9102", mdm.STEWARD) if hasattr(rt, "get_object_or_none") else None:
                    pass
                existing_sku = next(
                    (
                        item
                        for item in rt.list_objects("ContractSKU")
                        if item["object_id"] == "ERP-SKU-9102"
                    ),
                    None,
                )
                if existing_sku is not None:
                    act3_existing = {
                        "ok": True,
                        "act": 3,
                        "already_approved": True,
                        "spec_c_exists": True,
                    }
                    self._mark_act_done("3", act3_existing)
                    self._save_meta()
                    return {"act_result": act3_existing, "state": self.state_snapshot()}

                probe = self.try_register_sku(
                    {
                        "sku_id": "ERP-SKU-9102",
                        "invoice_title": "河北焦化新规格试采购",
                        "brand_id": "BRAND-2",
                        "unit_price_ton": 1400,
                        "ash_pct": 14.0,
                        "sulfur_pct": 1.0,
                        "request_new_spec_id": "SPEC-C",
                        "request_new_spec_name": "三级冶金焦 (CSR>=45%)",
                    }
                )["probe_result"]
                run_id = probe["run_id"]
            else:
                run_id = str(existing_req["run_id"])

            spec_c_exists = any(
                row["object_id"] == "SPEC-C" for row in rt.list_objects("MaterialSpec")
            )
            act3_summary = {
                "ok": True,
                "act": 3,
                "run_id": run_id,
                "status": "PENDING_APPROVAL",
                "spec_c_exists_in_mem": spec_c_exists,
                "awaiting_count": len(latent.awaiting_approval_moves()),
            }
            self._mark_act_done("3", act3_summary)
            self._save_meta()
            return {"act_result": act3_summary, "state": self.state_snapshot()}

    def decide_act_3(
        self,
        *,
        op: str = "approve",
        run_id: str | None = None,
        approver_id: str | None = None,
    ) -> dict[str, Any]:
        """MDM 管家签批或驳回 W_lat 中挂起的新物理品种申请。"""
        with self._lock:
            rt, latent = self._ensure_ready()
            target_run_id = str(run_id or "").strip()
            if not target_run_id:
                awaiting = latent.awaiting_approval_moves()
                if not awaiting:
                    return {
                        "ok": False,
                        "error": "W_lat 当前没有待签批的新规格申请",
                        "state": self.state_snapshot(),
                    }
                target_run_id = str(awaiting[0]["run_id"])

            steward = Actor(
                actor_id=str(approver_id or mdm.STEWARD.actor_id),
                role=mdm.STEWARD.role,
                clearance_tags=set(mdm.STEWARD.clearance_tags),
            )
            if op == "approve":
                move = latent.approve_move(target_run_id, steward)
                self._upsert_legacy_sku_source(move["params"])
                settled = latent.settle_move(
                    target_run_id,
                    readback_objects=self._legacy_snapshot_payload(),
                )
                spec_c_exists = any(
                    row["object_id"] == move["params"].get("request_new_spec_id")
                    or row["object_id"] == "SPEC-C"
                    for row in rt.list_objects("MaterialSpec")
                )
                res = {
                    "ok": True,
                    "op": "approve",
                    "run_id": target_run_id,
                    "status": settled["status"],
                    "approved_by": steward.actor_id,
                    "spec_c_exists_in_mem": spec_c_exists,
                }
                self._mark_act_done("3", {**(self.meta.get("acts") or {}).get("3", {}), **res})
                self._push_timeline(
                    "ACT 3 签批",
                    f"MDM 管家 {steward.actor_id} 批准新规格入库 ({settled['status']})",
                    f"post_approval 原子创建物理规格并完成 sku_maps_to_spec 关联。",
                    "ok",
                )
            elif op == "reject":
                rejected = latent.reject_move(
                    target_run_id, steward, reason="主数据管家驳回新规格申请"
                )
                res = {
                    "ok": True,
                    "op": "reject",
                    "run_id": target_run_id,
                    "status": rejected["status"],
                    "rejected_by": steward.actor_id,
                }
                self._push_timeline(
                    "ACT 3 驳回",
                    f"MDM 管家 {steward.actor_id} 驳回申请 ({target_run_id})",
                    "W_mem 与老 ERP 源表保持零修改。",
                    "warn",
                )
            else:
                return {
                    "ok": False,
                    "error": f"不支持的签批指令 {op!r}",
                    "state": self.state_snapshot(),
                }

            lp = self.meta.get("last_probe")
            if isinstance(lp, dict) and lp.get("run_id") == target_run_id:
                lp["status"] = res["status"]
            self._save_meta()
            return {"decision_result": res, "state": self.state_snapshot()}

    # -- Act 4 & Interactive Consumption / Lot Intake ----------------------
    def consume_brand(self, *, brand_id: str = "BRAND-1", tons: int = 150) -> dict[str, Any]:
        """车间报工系统只按厂家盲投（先进先出）扣减高维批次桶，或拦截超库存投料。"""
        with self._lock:
            rt, _ = self._ensure_ready()
            norm_brand = _normalize_brand_input(brand_id)
            run_id = self._next_run_id("MDM-CONS")
            try:
                settled = self._stage_and_settle_owned(
                    run_id,
                    "consume_by_brand_fifo",
                    {"brand_id": norm_brand, "tons": int(tons)},
                    mdm.MES,
                )
            except InvariantError as exc:
                res = {
                    "ok": True,
                    "blocked": True,
                    "status": "REJECTED",
                    "run_id": run_id,
                    "error_code": exc.code or "INVARIANT_VIOLATION",
                    "error_message": str(exc),
                }
                self._push_timeline(
                    "库存拦截",
                    f"车间按品牌 {brand_id} 盲投 {tons} 吨被拒 ({res['error_code']})",
                    res["error_message"],
                    "warn",
                )
                self._save_meta()
                return {"consume_result": res, "state": self.state_snapshot()}

            ledger = rt.call_function("reconcile_multi_view_ledger", {}, mdm.STEWARD)
            res = {
                "ok": True,
                "blocked": False,
                "status": settled["status"],
                "run_id": run_id,
                "brand_id": norm_brand,
                "tons": int(tons),
                "ledger": ledger,
            }
            self._push_timeline(
                "MES 盲投 FIFO",
                f"MES 消耗 {brand_id} 共 {tons} 吨（三账平衡 balanced={ledger['balanced']}）",
                f"总消耗 {ledger['totals']['tons']} 吨 / ¥{ledger['totals']['cost']:,}。",
                "ok",
            )
            self._save_meta()
            return {"consume_result": res, "state": self.state_snapshot()}

    def run_act_4(self) -> dict[str, Any]:
        """第四幕：死结二破局 — 车间只按 BRAND-1 盲投 150 吨，三视角数学恒等对账。"""
        with self._lock:
            rt, _ = self._ensure_ready()
            if not rt.list_objects("MaterialLot"):
                self.run_act_1()
            lots = rt.query_objects(mdm.MaterialLot, mdm.STEWARD)
            already_consumed = sum(lot.consumed_tons for lot in lots)
            if already_consumed == 0:
                consume_info = self.consume_brand(brand_id="BRAND-1", tons=150)["consume_result"]
            else:
                ledger = rt.call_function("reconcile_multi_view_ledger", {}, mdm.STEWARD)
                consume_info = {"ok": True, "already_consumed": already_consumed, "ledger": ledger}

            act4_summary = {
                "ok": True,
                "act": 4,
                "consume": consume_info,
                "ledger": rt.call_function("reconcile_multi_view_ledger", {}, mdm.STEWARD),
            }
            self._mark_act_done("4", act4_summary)
            self._save_meta()
            return {"act_result": act4_summary, "state": self.state_snapshot()}

    # -- Act 5: 1-Second Time-Machine Revert -------------------------------
    def revert_action_run(self, *, run_id: str | None = None) -> dict[str, Any]:
        """第五幕：1 秒时光机无损冲正指定或最近一条已生效业务变更。"""
        with self._lock:
            rt, _ = self._ensure_ready()
            target_run: dict[str, Any] | None = None
            runs = rt.list_action_runs("COMMITTED")
            if run_id:
                target_run = rt.get_action_run(str(run_id))
            else:
                # 优先撤回最近一条未冲正的 consume_by_brand_fifo，否则撤回最近一条非 revert 动作
                for candidate in reversed(runs):
                    if candidate.get("reverted_by_run_id"):
                        continue
                    if candidate.get("action_type") == "consume_by_brand_fifo":
                        target_run = candidate
                        break
                if target_run is None:
                    for candidate in reversed(runs):
                        if candidate.get("reverted_by_run_id"):
                            continue
                        if not str(candidate["run_id"]).endswith("-REV"):
                            target_run = candidate
                            break

            if target_run is None:
                return {
                    "ok": False,
                    "error": "当前没有可冲正的已提交变更",
                    "state": self.state_snapshot(),
                }

            target_id = str(target_run["run_id"])
            action_type = str(target_run["action_type"])
            actor = mdm.MES if action_type == "consume_by_brand_fifo" else mdm.STEWARD
            rev_id = self._next_run_id("MDM-REV")
            rev_run = rt.revert_action(target_id, rev_id, actor)

            if action_type == "register_contract_sku":
                sku_id = str((target_run.get("params") or {}).get("sku_id") or "")
                if sku_id:
                    self._remove_legacy_sku_source(sku_id)

            self._safe_rebase_if_clear()
            ledger = rt.call_function("reconcile_multi_view_ledger", {}, mdm.STEWARD)
            rev_summary = {
                "ok": True,
                "act": 5,
                "reverted_target_run_id": target_id,
                "reverted_action_type": action_type,
                "revert_run_id": rev_id,
                "revert_status": rev_run["status"],
                "ledger_after_revert": ledger,
            }
            self._mark_act_done("5", rev_summary)
            self._push_timeline(
                "ACT 5 冲正",
                f"时光机撤回 {target_id}（{action_type}）",
                f"批次库存与三本账无损恢复（当前总消耗 {ledger['totals']['tons']} 吨 / ¥{ledger['totals']['cost']}）。",
                "warn",
            )
            self._save_meta()
            return {"revert_result": rev_summary, "state": self.state_snapshot()}

    def run_walkthrough(self, *, include_revert: bool = False) -> dict[str, Any]:
        """一键按序贯通第 1~4 幕（可选包含第 5 幕冲正）。"""
        with self._lock:
            self.reset()
            self.run_act_1()
            self.run_act_2()
            self.run_act_3()
            self.decide_act_3(op="approve")
            self.run_act_4()
            if include_revert:
                self.revert_action_run()
            return self.state_snapshot()

    # -- Live Multi-Board State Snapshot -----------------------------------
    def state_snapshot(self) -> dict[str, Any]:
        """返回 W_real / W_mem / W_lat / 三视角对账账本与 ObjectSet 拓扑快照。"""
        with self._lock:
            rt, latent = self._ensure_ready()

            specs = [
                {
                    "spec_id": s.spec_id,
                    "name": s.name,
                    "ash_max_pct": s.ash_max_pct,
                    "sulfur_max_pct": s.sulfur_max_pct,
                }
                for s in sorted(
                    rt.query_objects(mdm.MaterialSpec, mdm.STEWARD),
                    key=lambda x: x.spec_id,
                )
            ]

            skus_view: list[dict[str, Any]] = []
            for sku in sorted(
                rt.query_objects(mdm.ContractSKU, mdm.STEWARD), key=lambda x: x.sku_id
            ):
                linked_specs = [
                    sp.spec_id
                    for sp in rt.get_linked(
                        sku, mdm.SKU_MAPS_TO_SPEC, mdm.MaterialSpec, mdm.STEWARD
                    )
                ]
                skus_view.append(
                    {
                        "sku_id": sku.sku_id,
                        "invoice_title": sku.invoice_title,
                        "brand_id": sku.brand_id,
                        "unit_price_ton": sku.unit_price_ton,
                        "ash_pct": sku.ash_pct,
                        "sulfur_pct": sku.sulfur_pct,
                        "canonical_spec_id": sku.canonical_spec_id,
                        "mapped_by": sku.mapped_by,
                        "linked_specs": linked_specs,
                    }
                )

            lots_view: list[dict[str, Any]] = []
            for lot in sorted(
                rt.query_objects(mdm.MaterialLot, mdm.STEWARD), key=lambda x: x.lot_id
            ):
                edge_specs = [
                    s.spec_id
                    for s in rt.get_linked(lot, mdm.LOT_HAS_SPEC, mdm.MaterialSpec, mdm.STEWARD)
                ]
                edge_brands = [
                    m.brand_id
                    for m in rt.get_linked(
                        lot, mdm.LOT_PRODUCED_BY, mdm.Manufacturer, mdm.STEWARD
                    )
                ]
                edge_skus = [
                    k.sku_id
                    for k in rt.get_linked(lot, mdm.LOT_BILLED_AS, mdm.ContractSKU, mdm.STEWARD)
                ]
                lots_view.append(
                    {
                        "lot_id": lot.lot_id,
                        "sku_id": lot.sku_id,
                        "spec_id": lot.spec_id,
                        "brand_id": lot.brand_id,
                        "initial_tons": lot.initial_tons,
                        "remaining_tons": lot.remaining_tons,
                        "consumed_tons": lot.consumed_tons,
                        "unit_price_ton": lot.unit_price_ton,
                        "edges": {
                            "lot_has_spec": edge_specs,
                            "lot_produced_by": edge_brands,
                            "lot_billed_as": edge_skus,
                        },
                    }
                )

            ledger = rt.call_function("reconcile_multi_view_ledger", {}, mdm.STEWARD)
            views = ledger["views"]
            totals = ledger["totals"]
            delta_tons = max(
                abs(views["brand_tons"] - totals["tons"]),
                abs(views["spec_tons"] - totals["tons"]),
                abs(views["sku_tons"] - totals["tons"]),
            )
            delta_cost = max(
                abs(views["brand_cost"] - totals["cost"]),
                abs(views["spec_cost"] - totals["cost"]),
                abs(views["sku_cost"] - totals["cost"]),
            )

            brand_lots = rt.search_set(mdm.MaterialLot, mdm.STEWARD, {"brand_id": "BRAND-1"})
            pivot_specs = rt.pivot(brand_lots, mdm.LOT_HAS_SPEC, mdm.STEWARD)
            pivot_skus = rt.pivot(brand_lots, mdm.LOT_BILLED_AS, mdm.STEWARD)
            agg_spec = rt.aggregate(brand_lots, group_by="spec_id", sum_field="consumed_tons")

            mem_runs = [
                {
                    "run_id": r["run_id"],
                    "action_type": r["action_type"],
                    "action_cn": _ACTION_CN.get(str(r["action_type"]), r["action_type"]),
                    "status": r["status"],
                    "approved_by": r.get("approved_by"),
                    "reverted_by_run_id": r.get("reverted_by_run_id"),
                    "error_code": r.get("error_code"),
                    "params": r.get("params"),
                }
                for r in rt.list_action_runs()[-12:]
            ]

            by_rid: dict[str, dict[str, Any]] = {}
            for item in list(self.meta.get("guardrail_history") or []):
                by_rid[str(item["run_id"])] = dict(item)
            for r in latent.latent_runtime.list_action_runs("REJECTED"):
                by_rid[str(r["run_id"])] = {
                    "run_id": r["run_id"],
                    "action_type": r["action_type"],
                    "sku_id": (r.get("params") or {}).get("sku_id"),
                    "error_code": r.get("error_code"),
                    "error_message": r.get("error_message"),
                    "executed_at": r.get("executed_at"),
                }
            lat_rejected = list(by_rid.values())[-8:]

            awaiting = [
                {
                    "run_id": mv["run_id"],
                    "action_type": mv["action_type"],
                    "params": mv.get("params"),
                }
                for mv in latent.awaiting_approval_moves()
            ]
            pending = [
                {
                    "run_id": mv["run_id"],
                    "action_type": mv["action_type"],
                    "params": mv.get("params"),
                }
                for mv in latent.pending_moves()
            ]

            snapshot = {
                "studio": {
                    "title": "业务本体沙盘 · 钢厂主数据治理两大死结可视化演播台",
                    "port": self.port,
                    "state_dir": str(self.state_dir),
                    "completed_acts": list(self.meta.get("completed_acts") or []),
                    "updated_at": self.meta.get("updated_at"),
                },
                "w_real": {
                    "schema_note": "外部老账套源表无标准物理规格列，也无独立物理规格表与高维批次表",
                    "reindex_count": int(self.legacy_erp.get("reindex_count", 0)),
                    "last_reindex_at": self.legacy_erp.get("last_reindex_at"),
                    "manufacturers": copy.deepcopy(self.legacy_erp.get("Manufacturer") or []),
                    "skus": copy.deepcopy(self.legacy_erp.get("ContractSKU") or []),
                },
                "w_mem": {
                    "db_path": str(self.mem_db_path),
                    "specs": specs,
                    "skus": skus_view,
                    "overlay_rows": self._overlay_rows(),
                    "lots": lots_view,
                    "recent_runs": mem_runs,
                },
                "w_lat": {
                    "db_path": str(self.lat_db_path),
                    "awaiting_approval_count": len(awaiting),
                    "awaiting_approval_moves": awaiting,
                    "pending_count": len(pending),
                    "pending_moves": pending,
                    "rejected_guardrail_runs": lat_rejected,
                    "last_probe": self.meta.get("last_probe"),
                },
                "ledger": {
                    **ledger,
                    "delta_tons": delta_tons,
                    "delta_cost": delta_cost,
                    "object_set_pivot": {
                        "brand_lot_ids": sorted(brand_lots.ids),
                        "pivot_spec_ids": sorted(pivot_specs.ids),
                        "pivot_sku_ids": sorted(pivot_skus.ids),
                        "spec_consumption": [
                            {"spec_id": row.key, "consumed_tons": row.metrics["sum"]}
                            for row in agg_spec.rows
                        ],
                    },
                },
                "acts": self.meta.get("acts") or {},
                "timeline": list(self.meta.get("timeline") or []),
            }
            snapshot["display"] = self._build_display_view(snapshot)
            return snapshot

    def _build_display_view(self, snap: dict[str, Any]) -> dict[str, Any]:
        """生成面向非技术投资人的 100% 零英文字母纯中文主数据治理视图。"""
        w_real = snap["w_real"]
        w_mem = snap["w_mem"]
        w_lat = snap["w_lat"]
        L = snap["ledger"]

        real_skus_cn = [
            {
                "sku_label": _cn_sku_id(k.get("sku_id")),
                "brand_cn": _cn_brand(k.get("brand_id")),
                "unit_price_ton": int(k.get("unit_price_ton") or 0),
                "invoice_title": _cn_invoice_title(k.get("invoice_title")),
                "ash_pct": float(k.get("ash_pct") or 0.0),
                "sulfur_pct": float(k.get("sulfur_pct") or 0.0),
            }
            for k in w_real.get("skus") or []
        ]

        overlay_by_id = {
            row["object_id"]: row["properties"]
            for row in w_mem.get("overlay_rows") or []
        }
        mem_skus_cn: list[dict[str, Any]] = []
        for k in w_mem.get("skus") or []:
            ov = overlay_by_id.get(k["sku_id"]) or {}
            spec_cn = _cn_spec(k.get("canonical_spec_id"))
            ov_spec_cn = _cn_spec(ov.get("canonical_spec_id")) if ov.get("canonical_spec_id") else "尚未贴片"
            mapped_by_cn = _cn_actor(ov.get("mapped_by"))
            links_cn = "、".join(_cn_spec(sp) for sp in (k.get("linked_specs") or [])) or "—"
            mem_skus_cn.append(
                {
                    "sku_label": _cn_sku_id(k.get("sku_id")),
                    "brand_cn": _cn_brand(k.get("brand_id")),
                    "is_mapped": bool(k.get("canonical_spec_id")),
                    "spec_cn": spec_cn,
                    "overlay_cn": f"{ov_spec_cn}（归并人：{mapped_by_cn}）",
                    "links_cn": links_cn,
                }
            )

        lots_cn: list[dict[str, Any]] = []
        for l in w_mem.get("lots") or []:
            edges = l.get("edges") or {}
            spec_list = "、".join(_cn_spec(x) for x in (edges.get("lot_has_spec") or [])) or "—"
            brand_list = "、".join(_cn_brand(x) for x in (edges.get("lot_produced_by") or [])) or "—"
            sku_list = "、".join(_cn_sku_id(x) for x in (edges.get("lot_billed_as") or [])) or "—"
            lots_cn.append(
                {
                    "lot_label": _cn_lot(l.get("lot_id")),
                    "unit_price_ton": int(l.get("unit_price_ton") or 0),
                    "remaining_tons": int(l.get("remaining_tons") or 0),
                    "consumed_tons": int(l.get("consumed_tons") or 0),
                    "edge_spec_cn": f"规格：{spec_list}",
                    "edge_brand_cn": f"厂家：{brand_list}",
                    "edge_sku_cn": f"原始码：{sku_list}",
                }
            )

        lp = w_lat.get("last_probe")
        probe_cn = None
        if isinstance(lp, dict):
            code = str(lp.get("error_code") or "")
            status_raw = str(lp.get("status") or "")
            status_map = {
                "PENDING_APPROVAL": "待主管签批",
                "COMMITTED": "已确认入账",
                "REJECTED": "已当场拦截",
            }
            tag_cn = _ERROR_CN.get(code) or status_map.get(status_raw, "已完成核验")
            msg_cn = (
                _cn_error_msg(code, str(lp.get("error_message") or ""))
                if lp.get("blocked")
                else f"当前流转状态：{status_map.get(status_raw, '已确认入账')}"
            )
            recs_cn = [
                f"{_cn_spec(r.get('spec_id'), full=True)}"
                for r in (lp.get("recommendations") or [])
            ]
            probe_cn = {
                "sku_label": _cn_sku_id(lp.get("sku_id")),
                "blocked": bool(lp.get("blocked")),
                "tag_cn": tag_cn,
                "message_cn": msg_cn,
                "recommendations_cn": " / ".join(recs_cn),
            }

        rej_cn = [
            {
                "run_label": _cn_run_label(r.get("run_id")),
                "sku_label": _cn_sku_id(r.get("sku_id")),
                "error_cn": _ERROR_CN.get(str(r.get("error_code") or ""), "违规拦截"),
                "message_cn": _cn_error_msg(
                    str(r.get("error_code") or ""), str(r.get("error_message") or "")
                ),
            }
            for r in reversed((w_lat.get("rejected_guardrail_runs") or [])[-3:])
        ]

        awaiting_cn = [
            {
                "run_id": mv["run_id"],
                "run_label": _cn_run_label(mv.get("run_id")),
                "new_spec_cn": _cn_spec(
                    (mv.get("params") or {}).get("request_new_spec_id"), full=True
                ),
                "sku_label": _cn_sku_id((mv.get("params") or {}).get("sku_id")),
            }
            for mv in w_lat.get("awaiting_approval_moves") or []
        ]

        views = L["views"]
        by_brand_cn = [
            {
                "title": f"{_cn_brand(r.get('brand_id'))}",
                "metric": f"{int(r['tons'])} 吨 · ¥{int(r['cost']):,}",
            }
            for r in L.get("by_brand") or []
        ]
        by_spec_cn = [
            {
                "title": f"{_cn_spec(r.get('spec_id'), full=True)}",
                "metric": f"{int(r['tons'])} 吨 · ¥{int(r['cost']):,}",
            }
            for r in L.get("by_spec") or []
        ]
        by_sku_cn = [
            {
                "title": f"{_cn_sku_id(r.get('sku_id'))} · {_cn_invoice_title(r.get('invoice_title'))}",
                "metric": f"{int(r['tons'])} 吨 · ¥{int(r['cost']):,}",
            }
            for r in L.get("by_sku") or []
        ]

        piv = L.get("object_set_pivot") or {}
        piv_lots = "、".join(_cn_lot(x) for x in (piv.get("brand_lot_ids") or [])) or "—"
        piv_specs = "、".join(_cn_spec(x) for x in (piv.get("pivot_spec_ids") or [])) or "—"
        piv_skus = "、".join(_cn_sku_id(x) for x in (piv.get("pivot_sku_ids") or [])) or "—"

        tl_cn: list[str] = []
        for x in (snap.get("timeline") or [])[-3:]:
            txt = (
                str(x.get("title") or "")
                .replace("BRAND-1", "山西焦化厂")
                .replace("BRAND-2", "河北煤化厂")
                .replace("SPEC-A", "一级冶金焦")
                .replace("SPEC-B", "二级冶金焦")
                .replace("SPEC-C", "三级特种焦")
                .replace("ERP-SKU-", "散码 ")
                .replace("UNMAPPED_PHYSICAL_SPEC", "未绑定标准规格")
                .replace("SPEC_QUALITY_MISMATCH", "煤质指标以次充好")
                .replace("INSUFFICIENT_BRAND_STOCK", "厂家可用库存不足")
                .replace("COMMITTED", "已确认入账")
                .replace("MDM", "主数据")
                .replace("MES", "车间报工系统")
                .replace("ERP", "老账套")
                .replace("W_lat", "推演棋盘")
                .replace("balanced=True", "三本账分毫不差")
            )
            txt = _strip_latin_tokens(txt).strip("- （）()")
            tl_cn.append(f"[{x.get('at', '')}] {txt}")

        return {
            "hero_title": "业务本体沙盘 · 钢厂主数据治理两大死结可视化演播台",
            "hero_sub": "死结一：老账套散码零改表外挂归并（夜间全量覆盖 100% 存活）｜ 死结二：采购按规格 / 车间按厂家先进先出盲投 / 三本账分毫不差",
            "completed_acts": list(snap["studio"]["completed_acts"]),
            "pills": {
                "reindex": f"老账套重刷：{int(w_real['reindex_count'])} 次",
                "overlay": f"外挂贴片：{len(w_mem['overlay_rows'])} 条存活",
                "awaiting": f"推演待签批：{int(w_lat['awaiting_approval_count'])} 条",
                "balanced": "三本账分毫不差（差额 ¥0）"
                if L["balanced"]
                else "三本账存在差额",
            },
            "w_real": {
                "schema_note": "外部老账套源表没有任何标准规格字段，也没有独立规格表或批次表，物理结构一字未改",
                "reindex_line": f"累计全量数据覆盖：{int(w_real['reindex_count'])} 次"
                + (
                    f"（最近：{w_real['last_reindex_at']}）"
                    if w_real.get("last_reindex_at")
                    else ""
                ),
                "skus_count": len(real_skus_cn),
                "skus": real_skus_cn,
            },
            "w_mem": {
                "specs_count": len(w_mem.get("specs") or []),
                "skus": mem_skus_cn,
                "lots_count": len(lots_cn),
                "lots": lots_cn,
            },
            "w_lat": {
                "rejected_count": len(w_lat.get("rejected_guardrail_runs") or []),
                "last_probe": probe_cn,
                "awaiting": awaiting_cn,
                "rejected_runs": rej_cn,
            },
            "ledger": {
                "total_badge": f"{int(L['totals']['tons'])} 吨 · ¥{int(L['totals']['cost']):,}",
                "balanced_headline": (
                    f"三本账分毫不差（吨数差 {int(L['delta_tons'])} 吨 · 金额差 ¥{int(L['delta_cost'])}）"
                    if L["balanced"]
                    else "三本账出现核对偏差"
                ),
                "equation_line": (
                    f"成本账：{int(views['brand_tons'])} 吨 / ¥{int(views['brand_cost']):,} "
                    f"== 采购账：{int(views['spec_tons'])} 吨 / ¥{int(views['spec_cost']):,} "
                    f"== 财税账：{int(views['sku_tons'])} 吨 / ¥{int(views['sku_cost']):,}"
                ),
                "by_brand": by_brand_cn,
                "by_spec": by_spec_cn,
                "by_sku": by_sku_cn,
                "pivot_summary": f"山西焦化厂批次：{piv_lots} → 穿透规格：{piv_specs} → 溯源散码：{piv_skus}",
            },
            "timeline_line": "  ｜  ".join(reversed(tl_cn)) or "演播台系统就绪",
            "footer_storage": "老账套源表与双记忆账库实时落盘保护中 · 已就绪",
        }


# ---------------------------------------------------------------------------
# Web UI (1920x1080 High-Contrast Interactive Console - 100% Pure Chinese)
# ---------------------------------------------------------------------------
def render_mdm_studio_html(initial_state: dict[str, Any]) -> str:
    state_json = json.dumps(initial_state, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>业务本体沙盘 · 钢厂主数据治理两大死结可视化演播台</title>
<style>
  :root {{
    --bg: #070b14;
    --panel: #0f172a;
    --border: #1e293b;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --cyan: #22d3ee;
    --emerald: #34d399;
    --amber: #fbbf24;
    --rose: #fb7185;
    --violet: #a78bfa;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: radial-gradient(circle at 85% 0%, #172554 0%, var(--bg) 55%);
    color: var(--text);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    padding: 16px 20px 22px;
  }}
  .hero {{
    display: flex; justify-content: space-between; align-items: center; gap: 14px;
    padding: 13px 18px;
    background: linear-gradient(135deg, rgba(15,23,42,.97), rgba(17,28,53,.97));
    border: 1px solid #263654; border-radius: 14px; margin-bottom: 12px;
  }}
  .hero h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 800; }}
  .hero .sub {{ color: var(--muted); font-size: 12.5px; }}
  .pills {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .pill {{
    padding: 5px 11px; border-radius: 999px; font-size: 12px; font-weight: 700;
    border: 1px solid #334155; background: #0a1223;
  }}
  .pill.cyan {{ border-color: rgba(34,211,238,.45); color: var(--cyan); }}
  .pill.emerald {{ border-color: rgba(52,211,153,.5); color: var(--emerald); }}
  .pill.amber {{ border-color: rgba(251,191,36,.5); color: var(--amber); }}
  .pill.violet {{ border-color: rgba(167,139,250,.5); color: var(--violet); }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; align-items: center; }}
  button {{
    border: 1px solid #334155; background: #16213b; color: var(--text);
    padding: 8px 13px; border-radius: 9px; font-size: 12.5px; font-weight: 700; cursor: pointer;
  }}
  button:hover {{ border-color: var(--cyan); background: #1d2d52; }}
  button.primary {{ background: linear-gradient(135deg, #0ea5e9, #0284c7); border-color: #38bdf8; color: #fff; }}
  button.emerald {{ background: linear-gradient(135deg, #10b981, #059669); border-color: #34d399; color: #041b13; }}
  button.amber {{ background: linear-gradient(135deg, #f59e0b, #d97706); border-color: #fbbf24; color: #1b1100; }}
  button.violet {{ background: linear-gradient(135deg, #8b5cf6, #6d28d9); border-color: #a78bfa; color: #fff; }}
  button.danger {{ background: linear-gradient(135deg, #e11d48, #be123c); border-color: #fb7185; color: #fff; }}
  button.done {{ box-shadow: 0 0 0 2px rgba(52,211,153,.65) inset; }}
  .grid-4 {{
    display: grid; grid-template-columns: 0.94fr 1.18fr 1.02fr 1.12fr; gap: 12px; align-items: stretch;
  }}
  .board {{
    background: var(--panel); border-radius: 13px; border: 1px solid var(--border);
    padding: 13px 14px; display: flex; flex-direction: column; gap: 10px; min-height: 675px;
  }}
  .board.cyan {{ border-color: rgba(34,211,238,.45); background: linear-gradient(180deg, rgba(10,36,54,.4), var(--panel) 26%); }}
  .board.emerald {{ border-color: rgba(52,211,153,.45); background: linear-gradient(180deg, rgba(9,45,35,.4), var(--panel) 26%); }}
  .board.amber {{ border-color: rgba(251,191,36,.45); background: linear-gradient(180deg, rgba(48,32,9,.38), var(--panel) 26%); }}
  .board.violet {{ border-color: rgba(167,139,250,.48); background: linear-gradient(180deg, rgba(38,24,66,.42), var(--panel) 26%); }}
  .bhead {{
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid #1e293b; padding-bottom: 8px;
  }}
  .bhead h2 {{ margin: 0; font-size: 15px; font-weight: 800; }}
  .tag {{ font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px; background: #1e293b; }}
  .tag.cyan {{ background: rgba(34,211,238,.15); color: #67e8f9; border: 1px solid rgba(34,211,238,.35); }}
  .tag.emerald {{ background: rgba(52,211,153,.15); color: #6ee7b7; border: 1px solid rgba(52,211,153,.35); }}
  .tag.amber {{ background: rgba(251,191,36,.15); color: #fde68a; border: 1px solid rgba(251,191,36,.35); }}
  .tag.rose {{ background: rgba(251,113,133,.18); color: #fda4af; border: 1px solid rgba(251,113,133,.4); }}
  .tag.violet {{ background: rgba(167,139,250,.18); color: #ddd6fe; border: 1px solid rgba(167,139,250,.4); }}
  .card {{ background: #0a1122; border: 1px solid #1e293b; border-radius: 10px; padding: 10px 11px; }}
  .ctitle {{ font-size: 12.5px; font-weight: 800; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; gap: 6px; }}
  .list {{ display: flex; flex-direction: column; gap: 6px; }}
  .row-box {{
    padding: 7px 9px; border-radius: 8px; background: #080f1d; border: 1px solid #1b2842; font-size: 11.5px;
  }}
  .row-top {{ display: flex; justify-content: space-between; font-weight: 700; margin-bottom: 2px; gap: 6px; }}
  .edge-row {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
  .edge-pill {{
    padding: 2px 6px; border-radius: 5px; font-size: 10.5px; font-weight: 700;
    background: rgba(34,211,238,.1); border: 1px solid rgba(34,211,238,.3); color: #67e8f9;
  }}
  .edge-pill.brand {{ background: rgba(251,191,36,.1); border-color: rgba(251,191,36,.35); color: #fde68a; }}
  .edge-pill.sku {{ background: rgba(167,139,250,.12); border-color: rgba(167,139,250,.35); color: #ddd6fe; }}
  .identity-ok {{
    background: rgba(16, 185, 129, 0.14); border: 1px solid rgba(52,211,153,.55);
    border-radius: 10px; padding: 9px 11px; color: #a7f3d0; font-size: 12px; font-weight: 700;
  }}
  .form-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 6px; }}
  .form-grid input, .form-grid select {{
    width: 100%; background: #050a14; color: var(--text); border: 1px solid #334155;
    border-radius: 6px; padding: 5px 7px; font-size: 11.5px;
  }}
  .footer-strip {{
    margin-top: 10px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px 14px; display: flex; justify-content: space-between;
    font-size: 11.5px; color: var(--muted); gap: 12px;
  }}
</style>
</head>
<body>
  <div class="hero">
    <div>
      <h1>业务本体沙盘 · 钢厂主数据治理两大死结可视化演播台</h1>
      <div class="sub">死结一：老账套散码零改表外挂归并（夜间全量覆盖 100% 存活）｜ 死结二：采购按规格 / 车间按厂家先进先出盲投 / 三本账分毫不差</div>
    </div>
    <div class="pills" id="hero-pills"></div>
  </div>

  <div class="controls">
    <button onclick="callApi('/api/reset', null, '重置钢厂沙盘')" class="danger">重置钢厂沙盘</button>
    <button onclick="callApi('/api/act/1', null, '第一幕外挂归并演示')" id="btn-act-1" class="primary">第一幕：散码外挂归并 &amp; 覆盖存活</button>
    <button onclick="callApi('/api/reindex', null, '夜间全量数据覆盖演练')" class="primary">模拟老账套夜间全量覆盖</button>
    <button onclick="callApi('/api/act/2', null, '第二幕源头护栏拦截')" id="btn-act-2" class="amber">第二幕：源头双硬护栏试探</button>
    <button onclick="callApi('/api/act/3', null, '第三幕新品种锁入草稿区')" id="btn-act-3" class="amber">第三幕：新品种锁入草稿区挂起</button>
    <button onclick="callApi('/api/act/3/approve', null, '主数据管家签批生效')" id="btn-act-3-appr" class="emerald">主数据管家批准入库</button>
    <button onclick="callApi('/api/act/4', null, '第四幕按厂家盲投百五十吨')" id="btn-act-4" class="violet">第四幕：按厂家盲投 150 吨 &amp; 三账核对</button>
    <button onclick="callApi('/api/act/5', null, '第五幕一秒时光机撤回')" id="btn-act-5" class="danger">第五幕：一秒时光机撤回</button>
    <button onclick="callApi('/api/walkthrough', null, '全剧连续贯通演示')">一键全剧贯通</button>
    <span id="status-msg" class="pill">已就绪 · 每 1.5 秒自动同步</span>
  </div>

  <div class="grid-4">
    <section class="board cyan" id="board-real"></section>
    <section class="board emerald" id="board-mem"></section>
    <section class="board amber" id="board-lat"></section>
    <section class="board violet" id="board-ledger"></section>
  </div>

  <div class="footer-strip">
    <div id="timeline-strip">演播台系统就绪</div>
    <div id="paths-strip">老账套源表与双记忆账库实时落盘保护中 · 已就绪</div>
  </div>

<script>
let CURRENT = {state_json};

function esc(v) {{
  return String(v ?? "").replace(/[&<>"']/g, s => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }}[s]));
}}

async function callApi(url, bodyObj, label) {{
  const msg = document.getElementById("status-msg");
  msg.textContent = "正在执行：" + (label || "业务指令") + " …";
  try {{
    const res = await fetch(url, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(bodyObj || {{}})
    }});
    const data = await res.json();
    if (data.state) {{
      CURRENT = data.state;
      render(true);
    }}
    msg.textContent = data.error ? ("操作被安全规则拦截") : ("已完成：" + (label || "业务指令"));
  }} catch (_err) {{
    msg.textContent = "网络通信异常，请重试";
  }}
}}

function fillPreset(kind) {{
  const setVal = (id, v) => {{ const el = document.getElementById(id); if (el) el.value = v; }};
  if (kind === "wild") {{
    setVal("p-sku", "散码 9100"); setVal("p-title", "无归属野码");
    setVal("p-brand", "山西焦化厂"); setVal("p-price", "1900");
    setVal("p-ash", "12.0"); setVal("p-sulfur", "0.6");
    setVal("p-spec", ""); setVal("p-new-spec", ""); setVal("p-new-name", "");
  }} else if (kind === "adulterated") {{
    setVal("p-sku", "散码 9101"); setVal("p-title", "以次充好挂一级冶金焦");
    setVal("p-brand", "山西焦化厂"); setVal("p-price", "2000");
    setVal("p-ash", "13.2"); setVal("p-sulfur", "0.8");
    setVal("p-spec", "一级冶金焦"); setVal("p-new-spec", ""); setVal("p-new-name", "");
  }} else if (kind === "newspec") {{
    setVal("p-sku", "散码 9102"); setVal("p-title", "河北焦化新规格试采购");
    setVal("p-brand", "河北煤化厂"); setVal("p-price", "1400");
    setVal("p-ash", "14.0"); setVal("p-sulfur", "1.0");
    setVal("p-spec", ""); setVal("p-new-spec", "三级特种焦"); setVal("p-new-name", "三级特种焦（热态强度 ≥ 45%）");
  }} else if (kind === "valid") {{
    setVal("p-sku", "散码 9105"); setVal("p-title", "合规绑定二级焦测试码");
    setVal("p-brand", "山西焦化厂"); setVal("p-price", "1620");
    setVal("p-ash", "13.1"); setVal("p-sulfur", "0.85");
    setVal("p-spec", "二级冶金焦"); setVal("p-new-spec", ""); setVal("p-new-name", "");
  }}
}}

function submitCustomProbe() {{
  const g = id => (document.getElementById(id)?.value || "").trim();
  callApi("/api/try_register_sku", {{
    sku_id: g("p-sku"),
    invoice_title: g("p-title"),
    brand_id: g("p-brand"),
    unit_price_ton: Number(g("p-price") || 0),
    ash_pct: Number(g("p-ash") || 0),
    sulfur_pct: Number(g("p-sulfur") || 0),
    canonical_spec_id: g("p-spec") || null,
    request_new_spec_id: g("p-new-spec") || null,
    request_new_spec_name: g("p-new-name") || null
  }}, "手工建码护栏试探");
}}

function submitCustomConsume() {{
  const b = (document.getElementById("c-brand")?.value || "山西焦化厂").trim();
  const t = Number(document.getElementById("c-tons")?.value || 0);
  callApi("/api/consume_brand", {{brand_id: b, tons: t}}, "车间按厂家盲投报工");
}}

function isEditingForm() {{
  const el = document.activeElement;
  return el && (el.tagName === "INPUT" || el.tagName === "SELECT");
}}

function render(forceAll) {{
  const d = CURRENT.display;
  if (!d) return;
  const done = new Set(d.completed_acts || []);
  ["1", "2", "3", "4", "5"].forEach(k => {{
    const btn = document.getElementById("btn-act-" + k);
    if (btn) btn.classList.toggle("done", done.has(k));
  }});

  document.getElementById("hero-pills").innerHTML = `
    <span class="pill cyan">${{esc(d.pills.reindex)}}</span>
    <span class="pill emerald">${{esc(d.pills.overlay)}}</span>
    <span class="pill amber">${{esc(d.pills.awaiting)}}</span>
    <span class="pill violet">${{esc(d.pills.balanced)}}</span>
  `;

  // Board 1: W_real
  const realSkus = (d.w_real.skus || []).map(k => `
    <div class="row-box">
      <div class="row-top"><span>${{esc(k.sku_label)}}</span><span class="tag cyan">${{esc(k.brand_cn)}} · ¥${{k.unit_price_ton}}/吨</span></div>
      <div>${{esc(k.invoice_title)}}</div>
      <div style="color:#94a3b8">灰分 ${{k.ash_pct}}% · 硫分 ${{k.sulfur_pct}}% · <span style="color:#fda4af">无标准物理规格列</span></div>
    </div>
  `).join("");
  document.getElementById("board-real").innerHTML = `
    <div class="bhead">
      <h2>① 真实棋盘（外部老账套源表）</h2>
      <span class="tag cyan">零改表</span>
    </div>
    <div class="card">
      <div style="font-size:11.5px;color:#cbd5e1;line-height:1.45">${{esc(d.w_real.schema_note)}}</div>
      <div style="margin-top:5px;color:#67e8f9;font-size:11.5px">${{esc(d.w_real.reindex_line)}}</div>
    </div>
    <div class="card" style="flex:1">
      <div class="ctitle"><span>采购物料码源表（仅原始票据列）</span><span class="tag cyan">${{d.w_real.skus_count}} 条</span></div>
      <div class="list">${{realSkus}}</div>
    </div>
  `;

  // Board 2: W_mem
  const memSkus = (d.w_mem.skus || []).map(k => `
    <div class="row-box">
      <div class="row-top"><span>${{esc(k.sku_label)}}</span><span class="tag ${{k.is_mapped ? 'emerald' : 'amber'}}">${{esc(k.spec_cn)}}</span></div>
      <div style="color:#94a3b8">${{esc(k.brand_cn)}} → 贴片账页：${{esc(k.overlay_cn)}} · 关联规格：${{esc(k.links_cn)}}</div>
    </div>
  `).join("");

  const lotCards = (d.w_mem.lots || []).map(l => `
    <div class="row-box">
      <div class="row-top"><span>${{esc(l.lot_label)}}（¥${{l.unit_price_ton}}/吨）</span><span class="tag emerald">余 ${{l.remaining_tons}} 吨 / 已耗 ${{l.consumed_tons}} 吨</span></div>
      <div class="edge-row">
        <span class="edge-pill">${{esc(l.edge_spec_cn)}}</span>
        <span class="edge-pill brand">${{esc(l.edge_brand_cn)}}</span>
        <span class="edge-pill sku">${{esc(l.edge_sku_cn)}}</span>
      </div>
    </div>
  `).join("");

  document.getElementById("board-mem").innerHTML = `
    <div class="bhead">
      <h2>② 记忆棋盘 · 外挂贴片账页与批次桶</h2>
      <span class="tag emerald">${{d.w_mem.specs_count}} 个标准规格</span>
    </div>
    <div class="card">
      <div class="ctitle"><span>采购物料码与外挂贴片账页</span><span class="tag emerald">全量覆盖不丢失</span></div>
      <div class="list">${{memSkus}}</div>
    </div>
    <div class="card" style="flex:1">
      <div class="ctitle"><span>高维原料批次桶（三边同步挂载）</span><span class="tag emerald">${{d.w_mem.lots_count}} 桶</span></div>
      <div class="list">${{lotCards || '<div style="color:#64748b;font-size:12px">点击第一幕建立高维原料批次桶</div>'}}</div>
    </div>
  `;

  // Board 3: W_lat Guardrails + Interactive Lab
  const lp = d.w_lat.last_probe;
  const probeBox = lp ? `
    <div class="row-box" style="border-color:${{lp.blocked ? 'rgba(251,113,133,.55)' : 'rgba(52,211,153,.55)'}}">
      <div class="row-top"><span>最近建码试探：${{esc(lp.sku_label)}}</span><span class="tag ${{lp.blocked ? 'rose' : 'emerald'}}">${{esc(lp.tag_cn)}}</span></div>
      <div style="color:#cbd5e1">${{esc(lp.message_cn)}}</div>
      ${{lp.recommendations_cn ? `<div style="color:#fde68a;margin-top:3px">智能规格推荐：${{esc(lp.recommendations_cn)}}</div>` : ""}}
    </div>
  ` : "";

  const rejRows = (d.w_lat.rejected_runs || []).map(r => `
    <div class="row-box">
      <div class="row-top"><span>${{esc(r.run_label)}}（${{esc(r.sku_label)}}）</span><span class="tag rose">${{esc(r.error_cn)}}</span></div>
      <div style="color:#94a3b8">${{esc(r.message_cn)}}</div>
    </div>
  `).join("");

  const awaitRows = (d.w_lat.awaiting || []).map(mv => `
    <div class="row-box" style="border-color:rgba(251,191,36,.6)">
      <div class="row-top"><span>${{esc(mv.run_label)}}（${{esc(mv.sku_label)}}）</span><span class="tag amber">待主管签批</span></div>
      <div>申请新增物理品种：<b>${{esc(mv.new_spec_cn)}}</b></div>
      <div style="margin-top:5px;display:flex;gap:6px">
        <button onclick="callApi('/api/act/3/approve', {{run_id:'${{esc(mv.run_id)}}'}}, '主数据管家签批生效')" class="emerald" style="padding:4px 9px;font-size:11px">主数据管家批准</button>
        <button onclick="callApi('/api/act/3/reject', {{run_id:'${{esc(mv.run_id)}}'}}, '主数据管家驳回申请')" class="danger" style="padding:4px 9px;font-size:11px">驳回申请</button>
      </div>
    </div>
  `).join("");

  if (!isEditingForm() || forceAll) {{
    document.getElementById("board-lat").innerHTML = `
      <div class="bhead">
        <h2>③ 推演棋盘（预演草稿区与硬护栏）</h2>
        <span class="tag amber">拦截 ${{d.w_lat.rejected_count}} 次</span>
      </div>
      <div class="card">
        <div class="ctitle"><span>破坏性增量建码实验台</span></div>
        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
          <button onclick="fillPreset('wild')" style="padding:3px 7px;font-size:11px">填野码 9100</button>
          <button onclick="fillPreset('adulterated')" style="padding:3px 7px;font-size:11px">填以次充好 9101</button>
          <button onclick="fillPreset('newspec')" style="padding:3px 7px;font-size:11px">填新品三级特种焦</button>
          <button onclick="fillPreset('valid')" style="padding:3px 7px;font-size:11px">填合规新码 9105</button>
        </div>
        <div class="form-grid" id="probe-form">
          <input id="p-sku" value="散码 9101" placeholder="物料散码（如：散码 9108）" />
          <input id="p-title" value="以次充好挂一级冶金焦" placeholder="采购发票名称" />
          <input id="p-brand" value="山西焦化厂" placeholder="供货厂家名称" />
          <input id="p-price" value="2000" placeholder="含税单价（¥/吨）" />
          <input id="p-ash" value="13.2" placeholder="灰分（%）" />
          <input id="p-sulfur" value="0.8" placeholder="硫分（%）" />
          <input id="p-spec" value="一级冶金焦" placeholder="绑定标准规格（可留空）" />
          <input id="p-new-spec" value="" placeholder="申请新品种（如：三级特种焦）" />
          <input id="p-new-name" value="" placeholder="新品种指标说明" style="grid-column:span 2" />
        </div>
        <button onclick="submitCustomProbe()" class="amber" style="width:100%;margin-top:6px;padding:6px">提交推演棋盘护栏试探</button>
      </div>
      ${{probeBox}}
      ${{awaitRows}}
      <div class="card" style="flex:1">
        <div class="ctitle"><span>护栏拦截审计记录（隔离留痕）</span></div>
        <div class="list">${{rejRows || '<div style="color:#64748b;font-size:12px">尚无被拦截试探</div>'}}</div>
      </div>
    `;
  }}

  // Board 4: 3-View Ledger
  const L = d.ledger;
  const renderRows = arr => (arr || []).map(r => `
    <div class="row-box">
      <div class="row-top"><span>${{esc(r.title)}}</span><span>${{esc(r.metric)}}</span></div>
    </div>
  `).join("") || '<div style="color:#64748b;font-size:11.5px">尚未发生投料消耗</div>';

  if (!isEditingForm() || forceAll) {{
    document.getElementById("board-ledger").innerHTML = `
      <div class="bhead">
        <h2>④ 成本 / 采购 / 财税三视角对账账本</h2>
        <span class="tag violet">${{esc(L.total_badge)}}</span>
      </div>
      <div class="identity-ok">
        ${{esc(L.balanced_headline)}}
        <div style="margin-top:4px;font-weight:500;color:#d1fae5;font-size:11.5px">${{esc(L.equation_line)}}</div>
      </div>
      <div class="card">
        <div class="ctitle"><span>视角一 · 成本核算视角（按供货厂家）</span></div>
        <div class="list">${{renderRows(L.by_brand)}}</div>
        <div class="ctitle" style="margin-top:8px"><span>视角二 · 采购与报产视角（按物理规格）</span></div>
        <div class="list">${{renderRows(L.by_spec)}}</div>
        <div class="ctitle" style="margin-top:8px"><span>视角三 · 财务与税务视角（按发票散码）</span></div>
        <div class="list">${{renderRows(L.by_sku)}}</div>
      </div>
      <div class="card" style="flex:1">
        <div class="ctitle"><span>高维多跳穿透溯源 &amp; 车间按厂家盲投</span></div>
        <div style="color:#cbd5e1;margin-bottom:6px;font-size:11.5px">${{esc(L.pivot_summary)}}</div>
        <div class="form-grid" id="consume-form">
          <input id="c-brand" value="山西焦化厂" placeholder="供货厂家" />
          <input id="c-tons" value="150" placeholder="报工吨数" />
        </div>
        <button onclick="submitCustomConsume()" class="violet" style="width:100%;margin-top:6px;padding:6px">执行车间按厂家先进先出盲投</button>
      </div>
    `;
  }}

  document.getElementById("timeline-strip").textContent = d.timeline_line;
  document.getElementById("paths-strip").textContent = d.footer_storage;
}}

render(true);
setInterval(async () => {{
  if (isEditingForm()) return;
  try {{
    const r = await fetch("/api/state");
    if (r.ok) {{
      CURRENT = await r.json();
      render(false);
    }}
  }} catch (_e) {{}}
}}, 1500);
</script>
</body>
</html>"""


class _MDMHandler(BaseHTTPRequestHandler):
    app: SteelMDMStudioApp

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = render_mdm_studio_html(self.app.state_snapshot()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.app.state_snapshot())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route {path}"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/api/reset":
                self._send_json(HTTPStatus.OK, {"ok": True, "state": self.app.reset()})
                return
            if path == "/api/act/1":
                self._send_json(HTTPStatus.OK, self.app.run_act_1())
                return
            if path == "/api/reindex":
                self._send_json(HTTPStatus.OK, self.app.reindex_legacy_erp())
                return
            if path == "/api/act/2":
                self._send_json(HTTPStatus.OK, self.app.run_act_2())
                return
            if path == "/api/try_register_sku":
                self._send_json(HTTPStatus.OK, self.app.try_register_sku(body))
                return
            if path == "/api/act/3":
                self._send_json(HTTPStatus.OK, self.app.run_act_3())
                return
            if path == "/api/act/3/approve":
                self._send_json(
                    HTTPStatus.OK,
                    self.app.decide_act_3(
                        op="approve",
                        run_id=body.get("run_id"),
                        approver_id=body.get("approver_id"),
                    ),
                )
                return
            if path == "/api/act/3/reject":
                self._send_json(
                    HTTPStatus.OK,
                    self.app.decide_act_3(
                        op="reject",
                        run_id=body.get("run_id"),
                        approver_id=body.get("approver_id"),
                    ),
                )
                return
            if path == "/api/act/4":
                self._send_json(HTTPStatus.OK, self.app.run_act_4())
                return
            if path == "/api/consume_brand":
                self._send_json(
                    HTTPStatus.OK,
                    self.app.consume_brand(
                        brand_id=str(body.get("brand_id") or "BRAND-1"),
                        tons=int(body.get("tons") or 0),
                    ),
                )
                return
            if path in ("/api/act/5", "/api/revert"):
                self._send_json(
                    HTTPStatus.OK,
                    self.app.revert_action_run(run_id=body.get("run_id")),
                )
                return
            if path == "/api/walkthrough":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "state": self.app.run_walkthrough(
                            include_revert=bool(body.get("include_revert", False))
                        ),
                    },
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route {path}"})
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc), "state": self.app.state_snapshot()},
            )


def create_server(
    app: SteelMDMStudioApp, *, host: str = "127.0.0.1", port: int = 8792
) -> ThreadingHTTPServer:
    class BoundHandler(_MDMHandler):
        pass

    BoundHandler.app = app
    return ThreadingHTTPServer((host, port), BoundHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="OpenOntology Steel MDM Two-Deadlocks Interactive Studio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--state-dir", default="/tmp/open_ontology_steel_mdm_studio")
    args = parser.parse_args(argv)

    app = SteelMDMStudioApp(args.state_dir, port=args.port)
    server = create_server(app, host=args.host, port=args.port)
    print(
        json.dumps(
            {
                "studio": "steel_mdm_ready",
                "url": f"http://{args.host}:{args.port}/",
                "state_dir": str(app.state_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
