"""OpenOntology 统一作战指挥中心（Command Center）。

同一个深色科技风单页应用里，同时管理：

* **Ontology 蓝图（PIM）** —— 纯静态元模型：ObjectTypes / Interfaces /
  LinkTypes / Functions / Actions / Automations，以及自动排布的内联 SVG 拓扑；
* **Instance 实例（Runtime + PSM）** —— 某个蓝图挂上数据库与外部系统连接器
  后的活体运行环境：实时世界状态、单步干跑、MCTS 多步沙盘、正式执行、
  审计账本、Approve / Revert、Fork 平行沙盘、一键拔除 SaaS。

向后兼容：``WorkshopServer(addr, runtime)`` 与 ``start_workshop_server(runtime)``
仍然原样可用 —— 内部会把单个 runtime 包装成「1 个默认蓝图 + 1 个默认实例」的
:class:`CommandCenterRegistry`。多实例场景传入 ``registry=`` 或使用
:func:`start_command_center_server`。

本模块是纯传输边界：不懂任何具体业务，只把 runtime / registry 的能力暴露成 JSON。
"""

from __future__ import annotations

import copy
import json
import threading
import types as _types
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Union, get_args, get_origin
from urllib.parse import parse_qs, urlparse

from .runtime import OntologyRuntime
from .spec import Actor, ObjectType, OntologySpec, OpenOntologyError
from .workshop_ui import (
    DISPLAY_JS,
    TOPOLOGY_JS,
    TOPOLOGY_PAGE_HTML,
    display_json,
)

__all__ = [
    "ConnectorMount",
    "OntologyCatalogEntry",
    "OntologyInstanceEntry",
    "CommandCenterRegistry",
    "WorkshopServer",
    "WorkshopHandle",
    "start_workshop_server",
    "start_command_center_server",
]


# ---------------------------------------------------------------------------
# 注册中心数据结构
# ---------------------------------------------------------------------------
@dataclass
class ConnectorMount:
    """后台数据库 / 企业资源计划 / 客户关系 / 软件服务 / 仓储 / 机械臂挂载状态。"""

    name: str
    kind: str
    endpoint: str
    status: str = "CONNECTED"  # CONNECTED | UNPLUGGED | STANDBY


@dataclass
class OntologyCatalogEntry:
    """Ontology 蓝图条目。"""

    ontology_id: str
    name: str
    category: str = "Enterprise"
    description: str = ""
    spec: OntologySpec = field(default_factory=OntologySpec)


@dataclass
class OntologyInstanceEntry:
    """Instance 实例化条目。"""

    instance_id: str
    ontology_id: str
    label: str
    description: str
    db_uri: str
    runtime: OntologyRuntime
    connectors: list[ConnectorMount] = field(default_factory=list)
    default_actor: Actor | None = None
    sample_action_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcts_config: dict[str, Any] | None = None
    unplug_fn: Callable[[], dict[str, Any]] | None = None


class CommandCenterRegistry:
    """多蓝图 / 多实例注册中心。"""

    def __init__(self) -> None:
        self.ontologies: dict[str, OntologyCatalogEntry] = {}
        self.instances: dict[str, OntologyInstanceEntry] = {}
        self.lock = threading.RLock()

    # -- registration ------------------------------------------------------
    def register_ontology(
        self,
        ontology_id: str,
        name: str,
        spec: OntologySpec,
        category: str = "Enterprise",
        description: str = "",
    ) -> OntologyCatalogEntry:
        entry = OntologyCatalogEntry(
            ontology_id=ontology_id,
            name=name,
            category=category,
            description=description,
            spec=spec,
        )
        self.ontologies[ontology_id] = entry
        return entry

    def register_instance(
        self,
        instance_id: str,
        ontology_id: str,
        label: str,
        runtime: OntologyRuntime,
        db_uri: str = "sqlite://:memory:",
        description: str = "",
        connectors: Iterable[ConnectorMount] | None = None,
        default_actor: Actor | None = None,
        sample_action_params: dict[str, dict[str, Any]] | None = None,
        mcts_config: dict[str, Any] | None = None,
        unplug_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> OntologyInstanceEntry:
        if ontology_id not in self.ontologies:
            raise KeyError(f"unknown ontology {ontology_id!r}")
        entry = OntologyInstanceEntry(
            instance_id=instance_id,
            ontology_id=ontology_id,
            label=label,
            description=description,
            db_uri=db_uri,
            runtime=runtime,
            connectors=list(connectors or []),
            default_actor=default_actor,
            sample_action_params=dict(sample_action_params or {}),
            mcts_config=dict(mcts_config) if mcts_config else None,
            unplug_fn=unplug_fn,
        )
        self.instances[instance_id] = entry
        return entry

    # -- lookup ------------------------------------------------------------
    def get_ontology(self, ontology_id: str) -> OntologyCatalogEntry:
        if ontology_id not in self.ontologies:
            raise KeyError(f"unknown ontology {ontology_id!r}")
        return self.ontologies[ontology_id]

    def get_instance(self, instance_id: str) -> OntologyInstanceEntry:
        if instance_id not in self.instances:
            raise KeyError(f"unknown instance {instance_id!r}")
        return self.instances[instance_id]

    def instances_for(self, ontology_id: str) -> list[OntologyInstanceEntry]:
        return [
            entry
            for entry in self.instances.values()
            if entry.ontology_id == ontology_id
        ]

    def default_instance(self) -> OntologyInstanceEntry:
        if not self.instances:
            raise KeyError("registry has no instances")
        return next(iter(self.instances.values()))

    def default_instance_id(self) -> str:
        return self.default_instance().instance_id

    # -- fork --------------------------------------------------------------
    def fork_instance(
        self,
        source_instance_id: str,
        new_instance_id: str | None = None,
        label: str | None = None,
    ) -> OntologyInstanceEntry:
        source = self.get_instance(source_instance_id)
        candidate = new_instance_id
        if not candidate:
            index = 1
            while f"{source.instance_id}-fork-{index}" in self.instances:
                index += 1
            candidate = f"{source.instance_id}-fork-{index}"
        if candidate in self.instances:
            raise KeyError(f"instance {candidate!r} already exists")

        forked = source.runtime.fork()
        entry = OntologyInstanceEntry(
            instance_id=candidate,
            ontology_id=source.ontology_id,
            label=label or f"{source.label} · 平行沙盘",
            description=f"由 {source.instance_id} 毫秒级克隆的独立沙盘实例",
            db_uri="sqlite://:memory: (fork)",
            runtime=forked,
            connectors=[
                ConnectorMount(
                    name=item.name,
                    kind=item.kind,
                    endpoint=item.endpoint,
                    status=item.status,
                )
                for item in source.connectors
            ],
            default_actor=source.default_actor,
            sample_action_params=copy.deepcopy(source.sample_action_params),
            mcts_config=dict(source.mcts_config) if source.mcts_config else None,
            unplug_fn=None,
        )
        self.instances[candidate] = entry
        return entry

    # -- compatibility -----------------------------------------------------
    @classmethod
    def from_runtime(
        cls,
        runtime: OntologyRuntime,
        *,
        actor: Actor | None = None,
        title: str | None = None,
    ) -> "CommandCenterRegistry":
        registry = cls()
        label = title or "Default Instance"
        registry.register_ontology(
            "default",
            title or "Ontology",
            runtime.spec,
            category="Enterprise",
            description="由单个 runtime 自动包装的兼容模式蓝图。",
        )
        registry.register_instance(
            "default",
            "default",
            label=label,
            runtime=runtime,
            db_uri=str(runtime.db_path),
            description="单 runtime 向后兼容实例。",
            default_actor=actor,
        )
        return registry


# ---------------------------------------------------------------------------
# 元模型投影
# ---------------------------------------------------------------------------
def _owned_field_names(cls: type[ObjectType]) -> list[str]:
    owned = cls.owned_spec()
    if isinstance(owned, dict):
        return sorted(owned)
    if owned is True:
        return sorted(cls.model_fields)
    return []


def _type_name(annotation: Any) -> str:
    """把 Python 注解渲染成简洁的类型名（``<class 'str'>`` → ``str``）。"""
    if annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        return " | ".join(_type_name(arg) for arg in get_args(annotation))
    if origin is not None:
        name = getattr(origin, "__name__", str(origin))
        args = ", ".join(_type_name(arg) for arg in get_args(annotation))
        return f"{name}[{args}]" if args else str(name)
    name = getattr(annotation, "__name__", None)
    return str(name) if name else str(annotation).replace("typing.", "")


def _spec_meta(spec: OntologySpec) -> dict[str, Any]:
    """把一个 OntologySpec 投影成前端可渲染的完整元模型。"""
    object_types = []
    for name, cls in spec.object_types.items():  # declaration order
        owned_fields = _owned_field_names(cls)
        masked = getattr(cls, "__masked_fields__", {}) or {}
        object_types.append(
            {
                "name": name,
                "primary_key": cls.primary_key_name(),
                "title": getattr(cls, "__title__", None),
                "fields": [
                    {
                        "name": field_name,
                        "type": _type_name(info.annotation),
                        "masked": field_name in masked,
                        "masked_roles": list(masked.get(field_name, ())),
                        "owned": field_name in owned_fields,
                    }
                    for field_name, info in cls.model_fields.items()
                ],
                "interfaces": list(getattr(cls, "__interfaces__", ()) or ()),
                "owned": bool(cls.owned_spec()),
                "owned_fields": owned_fields,
                "security_tag_fields": list(
                    getattr(cls, "__security_tag_fields__", ()) or ()
                ),
            }
        )
    interfaces = [
        {
            "name": decl.name,
            "required_fields": list(decl.required_fields),
            "description": decl.description,
            "implementations": [
                cls.__name__ for cls in spec.object_types_for_interface(decl)
            ],
        }
        for decl in sorted(spec.interfaces.values(), key=lambda d: d.name)
    ]
    return {
        "spec": spec.summary(),
        "object_types": object_types,
        "interfaces": interfaces,
        "link_types": [
            {
                "name": decl.name,
                "source": decl.source,
                "target": decl.target,
                "cardinality": decl.cardinality,
                "owned": decl.owned,
                "description": decl.description,
            }
            for decl in sorted(spec.link_types.values(), key=lambda d: d.name)
        ],
        "functions": [
            {"name": fn.name, "description": fn.description}
            for fn in sorted(spec.functions.values(), key=lambda d: d.name)
        ],
        "actions": [
            {
                "name": action.name,
                "description": action.description,
                "allowed_roles": list(action.allowed_roles),
                "approver_roles": list(action.approver_roles),
                "affects_source": action.affects_source,
                "params": action.params.model_json_schema(),
            }
            for action in sorted(spec.actions.values(), key=lambda d: d.name)
        ],
        "automations": [
            {
                "name": decl.name,
                "watch_object_type": decl.watch_object_type,
                "description": decl.description,
            }
            for decl in sorted(spec.automations.values(), key=lambda d: d.name)
        ],
    }


def _instance_summary(entry: OntologyInstanceEntry, registry: CommandCenterRegistry) -> dict[str, Any]:
    ontology = registry.ontologies.get(entry.ontology_id)
    runtime = entry.runtime
    return {
        "instance_id": entry.instance_id,
        "ontology_id": entry.ontology_id,
        "ontology_name": ontology.name if ontology else entry.ontology_id,
        "label": entry.label,
        "description": entry.description,
        "db_uri": entry.db_uri,
        "connectors": [asdict(item) for item in entry.connectors],
        "counts": {
            "objects": runtime.count_objects(),
            "links": _count_links(runtime),
            "runs": len(runtime.list_action_runs()),
        },
        "supports_mcts": bool(entry.mcts_config),
        "supports_unplug": entry.unplug_fn is not None,
        "default_actor_role": entry.default_actor.role if entry.default_actor else None,
        "sample_action_params": {
            key: value for key, value in entry.sample_action_params.items()
        },
    }


def _count_links(runtime: OntologyRuntime) -> int:
    row = runtime.connection.execute(
        "SELECT COUNT(*) AS n FROM ontology_links"
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# 前端单页应用
# ---------------------------------------------------------------------------
_WORKSHOP_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#05080f;--panel:#0c1420;--panel2:#101a28;--line:rgba(120,160,210,.16);
  --txt:#e6eefb;--dim:#8ea3bd;--cy:#38bdf8;--vi:#a78bfa;--gr:#22c55e;--am:#f5a524;--rd:#ef4444;
}
html,body{height:100%}
body{background:
  radial-gradient(1100px 560px at 6% -12%,rgba(56,189,248,.16),transparent 62%),
  radial-gradient(900px 520px at 98% -8%,rgba(129,140,248,.14),transparent 60%),
  linear-gradient(#05080f,#02040a);
  color:var(--txt);font-family:"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif;
  font-feature-settings:"tnum";overflow:hidden}
.shell{display:grid;grid-template-rows:auto 1fr;height:100vh}
header{display:flex;align-items:center;gap:16px;padding:11px 18px;
  border-bottom:1px solid var(--line);background:rgba(8,14,22,.86)}
.mark{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:19px;
  font-weight:800;color:#03121d;background:linear-gradient(135deg,#38bdf8,#6366f1);
  box-shadow:0 8px 22px rgba(56,189,248,.35)}
.brand b{font-size:15.5px;letter-spacing:.5px}
.brand span{display:block;font-size:9.5px;color:var(--dim);letter-spacing:2px;margin-top:2px}
.chips{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.chip{font-size:11px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);
  color:#b9cbe0;background:rgba(255,255,255,.03);white-space:nowrap}
.chip b{color:var(--cy)}
.role{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim)}
.role select{background:#0d1725;color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:6px 9px;font-size:12px;outline:none}
main{display:grid;grid-template-columns:330px 1fr;gap:10px;padding:10px;min-height:0}
.side{display:flex;flex-direction:column;gap:10px;min-height:0}
.stage{min-height:0;display:flex;flex-direction:column;gap:10px;overflow:auto}
.stage > *{flex-shrink:0}
.panel{background:linear-gradient(165deg,rgba(18,31,49,.9),rgba(8,14,24,.94));
  border:1px solid var(--line);border-radius:14px;display:flex;flex-direction:column;
  min-height:0;overflow:hidden;box-shadow:0 14px 36px rgba(0,0,0,.42)}
.ph{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:8px 12px;border-bottom:1px dashed var(--line);font-size:12.5px;font-weight:700;letter-spacing:.4px}
.ph small{font-weight:500;font-size:10px;color:var(--dim);letter-spacing:1px}
.ph i{color:var(--cy);font-style:normal;margin-right:6px}
.body{overflow:auto;padding:9px 11px;min-height:0}
.list{overflow:auto;padding:8px;display:flex;flex-direction:column;gap:7px;min-height:0}
.card{border:1px solid var(--line);border-left:3px solid rgba(120,160,210,.35);
  border-radius:10px;padding:8px 10px;background:rgba(255,255,255,.025);cursor:pointer;transition:.15s}
.card:hover{border-left-color:var(--cy);background:rgba(56,189,248,.06)}
.card.on{border-color:rgba(56,189,248,.6);border-left-color:var(--cy);background:rgba(56,189,248,.13);
  box-shadow:0 0 0 1px rgba(56,189,248,.2) inset}
.card h4{font-size:12.5px;font-weight:700;display:flex;justify-content:space-between;gap:8px}
.card h4 em{font-style:normal;font-family:"SF Mono",Menlo,monospace;font-size:10px;color:#8fb7d6}
.card .sub{font-size:10.5px;color:var(--dim);margin-top:3px;line-height:1.5}
.badges{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.badge{font-size:9.5px;padding:2px 6px;border-radius:5px;border:1px solid var(--line);color:#a9bdd4;background:rgba(255,255,255,.03)}
.badge.c{color:#a5e4ff;border-color:rgba(56,189,248,.35);background:rgba(56,189,248,.1)}
.badge.g{color:#bbf7d0;border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.1)}
.badge.v{color:#ddd6fe;border-color:rgba(167,139,250,.35);background:rgba(167,139,250,.1)}
.badge.a{color:#fde68a;border-color:rgba(245,165,36,.35);background:rgba(245,165,36,.1)}
.badge.r{color:#fecaca;border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.1)}
button{font-size:11px;padding:5px 10px;border-radius:7px;cursor:pointer;
  border:1px solid rgba(56,189,248,.42);background:rgba(56,189,248,.12);color:#cfeeff}
button:hover{background:rgba(56,189,248,.24)}
button.g{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.12);color:#c9f7d7}
button.g:hover{background:rgba(34,197,94,.24)}
button.a{border-color:rgba(245,165,36,.45);background:rgba(245,165,36,.12);color:#fde4b0}
button.r{border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.12);color:#ffd0d0}
button.v{border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.12);color:#e3dbff}
button.w{width:100%;margin-top:8px}
button:disabled{opacity:.4;cursor:not-allowed}
.hero{padding:14px 16px;border:1px solid var(--line);border-radius:14px;
  background:linear-gradient(150deg,rgba(20,36,58,.92),rgba(8,14,24,.96));box-shadow:0 14px 36px rgba(0,0,0,.42)}
.hero h2{font-size:17px;letter-spacing:.4px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.hero p{font-size:11.5px;color:var(--dim);margin-top:6px;line-height:1.65;max-width:980px}
.hero .acts{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.grid2{display:grid;grid-template-columns:1.15fr 1fr;gap:10px;min-height:0;align-items:start}
.grid2b{display:grid;grid-template-columns:1.05fr 1fr;gap:10px;min-height:0;align-items:start}
.panel.flat .body{overflow:auto;max-height:520px}
.mount{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 12px;border-bottom:1px solid var(--line);
  background:rgba(12,22,36,.7);font-size:11px;color:var(--dim)}
.mount b{color:var(--txt);font-size:12.5px}
.mount .sp{flex:1}
.fp{display:flex;gap:9px;flex-wrap:wrap}
.fp span{font-family:"SF Mono",Menlo,monospace;font-size:10.5px;color:#a5e4ff}
.tabs{display:flex;gap:5px;flex-wrap:wrap;padding:8px 11px;border-bottom:1px solid var(--line)}
.tab{font-size:11px;padding:4px 9px;border-radius:7px;border:1px solid var(--line);
  background:rgba(255,255,255,.02);color:#b9cbe0;cursor:pointer;white-space:nowrap}
.tab.on{background:rgba(56,189,248,.16);border-color:rgba(56,189,248,.55);color:#d6f2ff}
.obj{border:1px solid var(--line);border-left:3px solid rgba(120,160,210,.4);
  border-radius:10px;padding:7px 10px;margin-bottom:7px;background:rgba(255,255,255,.025)}
.obj:hover{border-left-color:var(--cy);background:rgba(56,189,248,.05)}
.obj h4{font-size:12px;font-weight:700;display:flex;justify-content:space-between;gap:8px}
.obj h4 span{font-family:"SF Mono",Menlo,monospace;font-size:10.5px;color:#a5e4ff;font-weight:500}
.kv{display:flex;gap:7px;font-size:10.8px;color:var(--dim);margin-top:3px;flex-wrap:wrap}
.kv b{color:#dbe7f5;font-weight:600;font-family:"SF Mono",Menlo,monospace;font-size:10.6px}
.kv .masked{color:var(--am)}
.kv .owned{color:#67e8f9}
.kv .ov{color:#fbbf24}
.ops{display:flex;gap:5px;margin-top:6px;flex-wrap:wrap}
.row{display:flex;gap:7px;align-items:center;margin-bottom:7px;flex-wrap:wrap}
label.f{display:flex;flex-direction:column;gap:3px;font-size:10px;color:var(--dim);flex:1;min-width:120px}
label.f input,label.f select{background:#0b1522;color:var(--txt);border:1px solid var(--line);
  border-radius:7px;padding:5px 8px;font-size:11px;font-family:"SF Mono",Menlo,monospace;outline:none}
select,input[type=text],textarea{background:#0b1522;color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:6px 9px;font-size:11px;font-family:"SF Mono",Menlo,monospace;outline:none}
pre{margin:0;font-family:"SF Mono",Menlo,monospace;font-size:10.6px;line-height:1.5;
  color:#9fb4cc;white-space:pre-wrap;word-break:break-all;max-height:240px;overflow:auto}
.run{border-left:3px solid var(--line);border-radius:8px;padding:6px 9px;margin-bottom:6px;
  background:rgba(255,255,255,.025);font-size:11px}
.run .id{font-family:"SF Mono",Menlo,monospace;color:#a5e4ff;font-size:10.5px}
.tag{font-size:9.5px;font-weight:700;padding:2px 6px;border-radius:5px;letter-spacing:.3px}
.t-COMMITTED{color:#bbf7d0;background:rgba(34,197,94,.2)}
.t-PENDING_APPROVAL{color:#fde68a;background:rgba(245,165,36,.2)}
.t-REJECTED{color:#fecaca;background:rgba(239,68,68,.2)}
.t-REVERTED{color:#ddd6fe;background:rgba(167,139,250,.2)}
.stat{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);padding:2px 0}
.stat b{color:#dbe7f5;font-family:"SF Mono",Menlo,monospace}
.hint{font-size:10.8px;color:var(--dim);line-height:1.6}
.err{color:#fca5a5}
.ok{color:#86efac}
.toast{position:fixed;right:18px;bottom:18px;max-width:520px;padding:10px 14px;border-radius:10px;
  font-size:12px;background:rgba(12,22,34,.97);border:1px solid var(--line);color:#dbe7f5;
  box-shadow:0 16px 40px rgba(0,0,0,.55);opacity:0;transform:translateY(8px);
  transition:all .18s ease;pointer-events:none;z-index:9}
.toast.show{opacity:1;transform:none}
.toast.bad{border-color:rgba(239,68,68,.55);color:#fecaca}
table{width:100%;border-collapse:collapse;font-size:10.8px}
th,td{text-align:left;padding:3px 6px;border-bottom:1px solid rgba(120,160,210,.1)}
th{color:#8ea3bd;font-weight:600;font-size:10px;letter-spacing:.4px}
td b{color:#dbe7f5;font-family:"SF Mono",Menlo,monospace;font-size:10.4px}
.sect{font-size:11.5px;font-weight:700;color:#cfe0f3;margin:9px 0 6px;letter-spacing:.3px}
.sect:first-child{margin-top:0}
.svgbox{background:rgba(4,9,16,.55);border:1px solid var(--line);border-radius:12px;padding:4px;overflow:auto}
.svgbox svg{display:block;width:100%;height:auto}
.en{font-family:"SF Mono",Menlo,monospace;font-size:9.5px;color:#6f839b;font-weight:500;letter-spacing:.2px}
.ph-link{display:flex;align-items:center;gap:8px;color:inherit;text-decoration:none;border-radius:6px;outline-offset:2px}
.ph-link:hover{color:#d6f2ff}
.ph-link small{font-weight:500;font-size:10px;color:var(--dim);letter-spacing:1px}
.ph-more{font-size:10.5px;font-weight:600;color:#a5e4ff;text-decoration:none;white-space:nowrap;
  border:1px solid rgba(56,189,248,.35);border-radius:6px;padding:3px 7px;background:rgba(56,189,248,.08)}
.ph-more:hover{background:rgba(56,189,248,.2)}
.topo-link{display:block;background:rgba(4,9,16,.55);border:1px solid var(--line);border-radius:12px;
  padding:4px;overflow:auto;cursor:zoom-in;text-decoration:none;outline-offset:3px}
.topo-link:hover{border-color:rgba(56,189,248,.45)}
.topo-link svg{display:block;width:100%;height:auto}
.fl{display:flex;align-items:baseline;gap:5px;font-size:10px;color:var(--dim)}
.fl em{font-style:normal;color:#6f839b;font-size:9.5px;font-family:"SF Mono",Menlo,monospace}
.diff{width:100%;border-collapse:collapse}
.diff td{vertical-align:top;font-family:"SF Mono",Menlo,monospace;font-size:10px}
.diff .b{color:#fca5a5}.diff .a{color:#86efac}
</style></head>
<body><div class="shell">
<header>
  <div class="mark">◈</div>
  <div class="brand"><b>__TITLE__</b><span>统一作战指挥中心 · 本体蓝图 × 运行实例</span></div>
  <div class="chips" id="chips"></div>
  <div class="role">角色
    <select id="role" onchange="loadAll()">
      <option value="admin">管理员</option>
      <option value="finance">财务人员</option>
      <option value="ops">业务操作员</option>
      <option value="auditor">审计员</option>
      <option value="dispatcher">调度员</option>
      <option value="factory_manager">研发负责人</option>
      <option value="system">系统</option>
      <option value="finance_manager">财务经理</option>
      <option value="ai_agent">智能助手</option>
      <option value="buyer">采购员</option>
      <option value="mdm_steward">主数据管理员</option>
    </select>
    <button class="v" onclick="runAutomations()">⚡ 运行自动化规则</button>
  </div>
</header>
<main>
  <aside class="side">
    <section class="panel" style="flex:1.02">
      <div class="ph"><span><i>▍</i>📐 本体蓝图列表</span><small>ONTOLOGY BLUEPRINTS</small></div>
      <div class="list" id="ontologies"></div>
    </section>
    <section class="panel" style="flex:1.25">
      <div class="ph"><span><i>▍</i>🌍 运行实例列表</span><small>RUNTIME INSTANCES</small></div>
      <div class="list" id="instances"></div>
      <div style="padding:8px 10px;border-top:1px dashed var(--line)">
        <button class="v w" onclick="forkInstance()">＋ 克隆沙盘（Fork 当前实例）</button>
        <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:7px">
          <input type="checkbox" id="filterToggle" onchange="renderLists()"> 仅显示当前蓝图的实例
        </label>
      </div>
    </section>
  </aside>
  <section class="stage" id="stage"></section>
</main>
</div>
<div class="toast" id="toast"></div>
<script>
__UI_BUNDLE__
__TOPOLOGY_JS__
const S = {catalog:null, ont:null, inst:null, meta:null, objects:{}, type:null,
           ontoDetail:{}, mcts:null, result:null};
const $ = (id) => document.getElementById(id);
const role = () => $('role').value;
function setRole(value) {
  if (!value) return;
  const sel = $('role');
  if (!Array.from(sel.options).some(o => o.value === value)) {
    const opt = document.createElement('option');
    opt.value = value; opt.textContent = zhRole(value);
    sel.appendChild(opt);
  }
  sel.value = value;
}

function toast(msg, bad) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3600);
}

async function api(path, body) {
  let url = path + (path.includes('?') ? '&' : '?') + 'role=' + encodeURIComponent(role());
  if (S.inst && !path.includes('instance=')) {
    url += '&instance=' + encodeURIComponent(S.inst.instance_id);
  }
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({instance: S.inst && S.inst.instance_id}, body))
  };
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({ok: false, error: 'invalid JSON'}));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function chips() {
  const c = S.catalog;
  const parts = [
    ['本体蓝图', c.ontologies.length],
    ['运行实例', c.instances.length],
    ['对象总数', c.instances.reduce((n, i) => n + i.counts.objects, 0)],
    ['动作执行记录', c.instances.reduce((n, i) => n + i.counts.runs, 0)]
  ];
  $('chips').innerHTML = parts.map(([k, n]) =>
    '<span class="chip">' + k + ' <b>' + n + '</b></span>').join('');
}

function renderLists() {
  if (!S.catalog) return;
  const only = $('filterToggle').checked && S.ont;
  const instances = S.catalog.instances.filter(i => !only || i.ontology_id === S.ont.ontology_id);
  $('ontologies').innerHTML = S.catalog.ontologies.map(o =>
    '<div class="card' + (S.ont && S.ont.ontology_id === o.ontology_id ? ' on' : '') +
    '" onclick="selectOntology(\'' + o.ontology_id + '\')">' +
    '<h4>' + esc(zh('ontologies', o.ontology_id, o.name)) + '<em>' + esc(zh('categories', o.category, o.category)) + '</em></h4>' +
    '<div class="sub">' + esc(o.description || '') + '</div>' +
    '<div class="badges"><span class="badge c">' + o.counts.object_types + ' 对象类型</span>' +
    '<span class="badge">' + o.counts.link_types + ' 关系类型</span>' +
    '<span class="badge a">' + o.counts.actions + ' 业务动作</span>' +
    '<span class="badge v">' + o.counts.instances + ' 运行实例</span></div></div>').join('');

  $('instances').innerHTML = instances.map(i => {
    const conns = (i.connectors || []).map(c =>
      '<span class="badge ' + (c.status === 'CONNECTED' ? 'g' : 'v') + '" title="' + esc(c.endpoint) + '">' +
      (c.status === 'CONNECTED' ? '🟢' : '🟣') + ' ' + esc(zhKind(c.kind)) + '：' + esc(c.name) +
      '（' + esc(zhStatus(c.status)) + '）</span>').join('');
    return '<div class="card' + (S.inst && S.inst.instance_id === i.instance_id ? ' on' : '') +
      '" onclick="selectInstance(\'' + i.instance_id + '\')">' +
      '<h4>' + esc(i.label) + '<em>' + esc(zh('ontologies', i.ontology_id, i.ontology_name)) + '</em></h4>' +
      '<div class="sub">数据源 ' + esc(i.db_uri) + '</div>' +
      '<div class="badges"><span class="badge">' + i.counts.objects + ' 对象</span>' +
      '<span class="badge">' + i.counts.links + ' 关系</span>' +
      '<span class="badge">' + i.counts.runs + ' 执行记录</span>' +
      (i.supports_mcts ? '<span class="badge c">多步推演</span>' : '') +
      (i.supports_unplug ? '<span class="badge a">可断开外部系统</span>' : '') + conns + '</div></div>';
  }).join('') || '<div class="hint">该蓝图暂无运行实例。</div>';
}

async function selectOntology(id) {
  S.ont = S.catalog.ontologies.find(o => o.ontology_id === id) || null;
  S.inst = null;
  S.meta = null;
  renderLists();
  if (!S.ont) return;
  if (!S.ontoDetail[id]) {
    S.ontoDetail[id] = await api('/api/ontology/' + encodeURIComponent(id));
  }
  renderOntologyView(S.ontoDetail[id]);
}

async function selectInstance(id) {
  const summary = S.catalog.instances.find(i => i.instance_id === id);
  if (!summary) return;
  S.inst = summary;
  S.ont = S.catalog.ontologies.find(o => o.ontology_id === summary.ontology_id) || null;
  S.meta = null;
  S.objects = {};
  S.type = null;
  S.mcts = null;
  if (summary.default_actor_role) setRole(summary.default_actor_role);
  renderLists();
  await loadInstanceData();
}

async function loadAll() {
  try {
    S.catalog = await api('/api/catalog');
    chips();
    if (S.inst) {
      const fresh = S.catalog.instances.find(i => i.instance_id === S.inst.instance_id);
      if (fresh) S.inst = fresh;
    }
    renderLists();
    if (S.inst) await loadInstanceData();
    else if (S.ont) await selectOntology(S.ont.ontology_id);
    else if (S.catalog.ontologies.length) await selectOntology(S.catalog.ontologies[0].ontology_id);
  } catch (e) { toast('加载失败：' + e.message, true); }
}

/* ------------------------- 蓝图定义视图 ------------------------- */
function renderOntologyView(detail) {
  const o = detail.ontology;
  const m = detail;
  const first = S.catalog.instances.find(i => i.ontology_id === o.ontology_id);
  const svg = topology(m);
  const topoHref = '/topology?ontology=' + encodeURIComponent(o.ontology_id);
  const objects = m.object_types.map(t => {
    const rows = t.fields.map(f => {
      const tags = [];
      if (f.name === t.primary_key) tags.push('<span class="badge c">主键</span>');
      if (f.owned) tags.push('<span class="badge a">本体自有</span>');
      if (f.masked) tags.push('<span class="badge r">🔒 不可见：' + esc((f.masked_roles||[]).map(zhRole).join(' / ')) + '</span>');
      return '<tr><td><b>' + esc(zhField(t.name, f.name)) + '</b> <span class="en">' + esc(f.name) +
        '</span></td><td>' + esc(f.type) + '</td><td>' + tags.join(' ') + '</td></tr>';
    }).join('');
    return '<div class="sect">📦 ' + esc(zhObject(t.name)) + ' <span class="badge ' + (t.owned ? 'a' : '') + '">' +
      (t.owned ? '本体自有' : '来自外部数据源') + '</span> <span class="en">' + esc(t.name) + '</span></div>' +
      '<table><tr><th>字段</th><th>类型</th><th>主键 / 主权 / 可见范围</th></tr>' + rows + '</table>';
  }).join('');

  const links = m.link_types.map(l =>
    '<div class="kv" style="margin:5px 0"><b>' + esc(zh('links', l.name, l.name)) + '</b><span>' +
    esc(zhObject(l.source)) + ' → ' + esc(zhObject(l.target)) + '</span><span class="badge">' +
    esc(zhCardinality(l.cardinality)) + '</span><span class="badge ' + (l.owned ? 'a' : '') + '">' +
    (l.owned ? '本体自有' : '来自外部数据源') + '</span><span class="en">' + esc(l.name) + '</span></div>').join('') ||
    '<div class="hint">无。</div>';

  const funcs = m.functions.map(f => '<div class="kv" style="margin:5px 0"><b>' + esc(zh('functions', f.name, f.name)) +
    '</b><span>' + esc(f.description) + '</span><span class="en">' + esc(f.name) + '</span></div>').join('') ||
    '<div class="hint">无。</div>';
  const autos = m.automations.map(a => '<div class="kv" style="margin:5px 0"><b>' + esc(zh('automations', a.name, a.name)) +
    '</b><span>监听对象：' + esc(zhObject(a.watch_object_type)) + '</span><span>' + esc(a.description) + '</span>' +
    '<span class="en">' + esc(a.name) + '</span></div>').join('') || '<div class="hint">无。</div>';

  const actions = m.actions.map(a => {
    const props = (a.params && a.params.properties) || {};
    const fields = Object.keys(props).map(k => '<span class="badge">' + esc(zhField(null, k)) + '：' +
      esc((props[k]||{}).type || 'any') + '</span>').join('');
    return '<div class="obj"><h4>' + esc(zhAction(a.name)) + ' <span class="en">' + esc(a.name) + '</span></h4>' +
      '<div class="sub hint">' + esc(a.description) + '</div>' +
      '<div class="badges">' + fields + '</div>' +
      '<div class="badges"><span class="badge c">可执行角色 ' + esc((a.allowed_roles||[]).map(zhRole).join(' / ')) + '</span>' +
      '<span class="badge a">审批角色 ' + esc((a.approver_roles||[]).map(zhRole).join(' / ') || '无需审批') + '</span>' +
      '<span class="badge">' + (a.affects_source ? '来自外部数据源' : '本体自有') + '</span></div></div>';
  }).join('') || '<div class="hint">无。</div>';

  $('stage').innerHTML =
    '<div class="hero"><h2>📐 ' + esc(zh('ontologies', o.ontology_id, o.name)) +
    ' <span class="badge c">' + esc(zh('categories', o.category, o.category)) + '</span></h2>' +
    '<p>' + esc(o.description) + '</p><div class="acts">' +
    (first ? '<button class="g" onclick="selectInstance(\'' + first.instance_id + '\')">🚀 进入该蓝图的首个运行实例</button>' : '') +
    '<span class="badge">' + m.counts.object_types + ' 对象类型</span>' +
    '<span class="badge">' + m.counts.interfaces + ' 共享接口</span>' +
    '<span class="badge">' + m.counts.link_types + ' 关系类型</span>' +
    '<span class="badge">' + m.counts.functions + ' 查询与计算</span>' +
    '<span class="badge">' + m.counts.actions + ' 业务动作</span>' +
    '<span class="badge">' + m.counts.automations + ' 自动化规则</span></div></div>' +
    '<div class="grid2"><section class="panel"><div class="ph">' +
    '<a class="ph-link" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0" ' +
    'title="在新标签页打开可缩放的本体关系图"><i>▍</i>本体关系图<small>METAMODEL TOPOLOGY</small></a>' +
    '<a class="ph-more" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0">查看大图 ↗</a></div>' +
    '<div class="body"><a class="topo-link" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0" ' +
    'title="在新标签页打开可缩放的本体关系图">' + svg + '</a></div></section>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>📦 对象类型与共享接口</span><small>SCHEMA</small></div>' +
    '<div class="body">' + objects + '</div></section></div>' +
    '<div class="grid2b"><section class="panel flat"><div class="ph"><span><i>▍</i>🔗 关系类型</span><small>RELATIONS</small></div>' +
    '<div class="body">' + links + '</div></section>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🧮 查询与计算 / ⚡ 自动化规则</span><small>READ & REFLEX</small></div>' +
    '<div class="body"><div class="sect">查询与计算</div>' + funcs + '<div class="sect">自动化规则</div>' + autos + '</div></section></div>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🎯 业务动作</span><small>GOVERNED ACTIONS</small></div>' +
    '<div class="body">' + actions + '</div></section>';
}

function topology(m) {
  if (!(m.object_types || []).length) return '<div class="hint">该本体暂无对象类型。</div>';
  return topologySvg(m);
}

/* ------------------------- 实例作战室视图 ------------------------- */
async function loadInstanceData() {
  if (!S.inst) return;
  try {
    const meta = await api('/api/meta');
    S.meta = meta;
    if (!S.type || !meta.object_types.some(t => t.name === S.type)) {
      S.type = meta.object_types[0] && meta.object_types[0].name;
    }
    S.objects = {};
    for (const t of meta.object_types) {
      const res = await api('/api/objects/' + encodeURIComponent(t.name));
      S.objects[t.name] = res.objects;
    }
    const fp = await api('/api/fingerprint');
    const runs = (await api('/api/action-runs')).runs;
    const links = (await api('/api/links')).links;
    renderInstanceView(meta, fp, runs, links);
  } catch (e) { toast('实例加载失败：' + e.message, true); }
}

function connectorChips(conns) {
  return (conns || []).map(c => '<span class="badge ' + (c.status === 'CONNECTED' ? 'g' : 'v') + '" title="' +
    esc(c.endpoint) + '">' + (c.status === 'CONNECTED' ? '🟢' : '🟣') + ' ' + esc(zhKind(c.kind)) + '：' + esc(c.name) +
    '（' + esc(zhStatus(c.status)) + '）</span>').join('') || '<span class="badge">无外部连接器</span>';
}

function renderInstanceView(meta, fp, runs, links) {
  const i = S.inst;
  const objectTabs = meta.object_types.map(t =>
    '<span class="tab' + (t.name === S.type ? ' on' : '') + '" data-name="' + esc(t.name) +
    '" onclick="pickType(\'' + t.name + '\')">' +
    esc(zhObject(t.name)) + ' <b style="color:#7f93ab">' + ((S.objects[t.name] || []).length) + '</b></span>').join('');
  const ifaceTabs = meta.interfaces.map(f =>
    '<span class="tab" data-name="' + esc(f.name) + '" onclick="pickInterface(\'' + f.name + '\')">◇ ' +
    esc(zh('interfaces', f.name, f.name)) + '</span>').join('');

  $('stage').innerHTML =
    '<div class="hero"><h2>🌍 ' + esc(i.label) + ' <span class="badge c">' +
    esc(zh('ontologies', i.ontology_id, i.ontology_name)) + '</span></h2>' +
    '<p>' + esc(i.description || '') + ' · <b style="color:#a5e4ff">数据源 ' + esc(i.db_uri) + '</b></p>' +
    '<div class="acts">' + connectorChips(i.connectors) + '</div></div>' +
    '<section class="panel"><div class="mount"><b>实例环境与后台挂载栏</b>' +
    '<span class="badge">' + esc(i.instance_id) + '</span><span class="sp"></span>' +
    '<small style="color:#8ea3bd;font-size:9.5px;letter-spacing:1px">当前状态指纹</small>' +
    '<div class="fp" id="fp"></div>' +
    '<button class="v" onclick="runAutomations()">🔄 触发自动化规则</button>' +
    '<button class="v" onclick="forkInstance()">🍴 克隆沙盘</button>' +
    (i.supports_unplug ? '<button class="r" onclick="unplug()">🔌 断开外部系统</button>' : '') +
    '</div></section>' +
    '<div class="grid2"><section class="panel"><div class="ph"><span><i>▍</i>🌍 当前业务状态</span><small>OBJECT EXPLORER · WORLD STATE</small></div>' +
    '<div class="tabs">' + objectTabs + ifaceTabs + '</div><div class="body" id="objects"></div></section>' +
    '<section class="panel"><div class="ph"><span><i>▍</i>🎯 业务动作与沙盘推演台</span><small>GOVERNED ACTION</small></div>' +
    '<div class="body"><div class="row"><select id="action" onchange="fillParams()"></select>' +
    '<span class="badge" id="actmeta"></span></div><div id="form" class="row"></div>' +
    '<div class="row"><button id="simBtn" onclick="simulate()">🔍 沙盘预演</button>' +
    '<button class="v" onclick="runMcts()">🌳 多步智能推演</button>' +
    '<button class="g" id="submitBtn" onclick="submitAction()">⚡ 正式执行</button></div>' +
    '<pre id="result" class="hint">沙盘预演或正式执行后，这里显示对象变更 / 错误码。</pre>' +
    '<div id="mcts" class="hint" style="margin-top:8px"></div>' +
    '<div class="hint" style="margin-top:8px">⚡ 自动化规则按（规则、对象、版本）去重执行。</div>' +
    '</div></section></div>' +
    '<div class="grid2b"><section class="panel"><div class="ph"><span><i>▍</i>🔗 语义关系与 🧮 查询计算</span><small>SEMANTIC EDGES</small></div>' +
    '<div class="body" id="links"></div></section>' +
    '<section class="panel"><div class="ph"><span><i>▍</i>📋 动作执行记录</span><small>ACTION RUNS</small></div>' +
    '<div class="body" id="runs"></div></section></div>';

  const sel = $('action');
  sel.innerHTML = meta.actions.map(a => '<option value="' + a.name + '">' + esc(zhAction(a.name)) + '</option>').join('');
  if (S.lastAction && meta.actions.some(a => a.name === S.lastAction)) sel.value = S.lastAction;
  renderFingerprint(fp);
  renderObjects();
  renderLinks(links);
  renderRuns(runs);
  fillParams();
}

function renderFingerprint(fp) {
  const box = $('fp');
  if (!box) return;
  box.innerHTML = '<span>对象 <b style="color:#dbe7f5">' + fp.objects + '</b></span>' +
    '<span>关系 <b style="color:#dbe7f5">' + fp.links + '</b></span>' +
    '<span>叠加层 <b style="color:#dbe7f5">' + fp.overlay + '</b></span>' +
    '<span>执行记录 <b style="color:#dbe7f5">' + fp.runs + '</b></span>';
}

function pickType(name) {
  S.type = name;
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('on'));
  renderObjects();
  const target = document.querySelector('.tab[data-name="' + name + '"]');
  if (target) target.classList.add('on');
  fillParams();
}

async function pickInterface(name) {
  try {
    const data = await api('/api/interface?interface=' + encodeURIComponent(name));
    const rows = (data.objects || []).map(o => '<div class="obj"><h4>◇ ' + esc(name) +
      '<span>' + esc(o[Object.keys(o)[0]]) + '</span></h4><div class="kv">' +
      Object.keys(o).map(k => '<span>' + esc(k) + ' <b>' + esc(typeof o[k] === 'object' ? JSON.stringify(o[k]) : o[k]) + '</b></span>').join('') +
      '</div></div>').join('');
    $('objects').innerHTML = '<div class="hint" style="margin-bottom:7px">Interface 多态视图：' + esc(name) + '</div>' +
      (rows || '<div class="hint">该接口暂无对象。</div>');
  } catch (e) { toast(e.message, true); }
}

function renderObjects() {
  const box = $('objects');
  if (!box) return;
  const list = S.objects[S.type] || [];
  const typeMeta = S.meta.object_types.find(t => t.name === S.type) || {fields: []};
  if (!list.length) { box.innerHTML = '<div class="hint">该类型暂无可见对象。</div>'; return; }
  box.innerHTML = list.map((o, idx) => {
    const fields = typeMeta.fields.map(f => {
      const v = o[f.name];
      const masked = v === '***MASKED***' || v === 'REDACTED';
      const cls = masked ? 'masked' : (f.owned ? 'owned' : '');
      let text = (v === null || v === undefined) ? '—'
        : (typeof v === 'object' ? JSON.stringify(v) : String(v));
      if (masked) text = '不可见';
      else text = zhValue(f.name, text);
      return '<span title="' + esc(f.name) + '">' + esc(zhField(S.type, f.name)) +
        ' <b class="' + cls + '">' + esc(text) + '</b></span>';
    }).join('');
    const pk = o[typeMeta.primary_key || Object.keys(o)[0]];
    return '<div class="obj"><h4>' + esc(zhObject(S.type)) + '<span>' + esc(pk) + '</span></h4>' +
      '<div class="kv">' + fields + '</div>' +
      '<div class="ops"><button onclick="pickObject(' + idx + ')">载入参数</button>' +
      '<button class="v" onclick="quickSim(' + idx + ')">沙盘预演</button></div></div>';
  }).join('');
}

function renderLinks(links) {
  const box = $('links');
  if (!box) return;
  const rows = (links || []).slice(0, 120).map(l =>
    '<div class="kv" style="margin:4px 0"><b>' + esc(zh('links', l.link_type, l.link_type)) + '</b><span>' +
    esc(zhObject(l.source_type)) + '：' + esc(l.source_id) + ' → ' + esc(zhObject(l.target_type)) + '：' +
    esc(l.target_id) + '</span></div>').join('');
  const funcs = (S.meta.functions || []).map(f =>
    '<div class="ops" style="margin:4px 0"><span class="badge">' + esc(zh('functions', f.name, f.name)) + '</span>' +
    '<span class="en">' + esc(f.name) + '</span>' +
    '<button onclick="callFunction(\'' + f.name + '\')">调用</button></div>').join('');
  box.innerHTML = '<div class="sect">🔗 语义关系</div>' + (rows || '<div class="hint">暂无关系边。</div>') +
    '<div class="sect">🧮 查询与计算（点击实时调用）</div>' + (funcs || '<div class="hint">无。</div>') +
    '<pre id="fnout" class="hint" style="margin-top:6px"></pre>';
}

async function callFunction(name) {
  try {
    const data = await api('/api/function', {function: name, params: {}});
    const el = $('fnout');
    if (el) el.textContent = name + ' →\n' + JSON.stringify(data.result, null, 2);
    toast('函数 ' + name + ' 返回');
  } catch (e) { toast(e.message, true); }
}

function currentAction() {
  return (S.meta.actions || []).find(a => a.name === $('action').value);
}

function typeOf(prop) {
  if (!prop) return 'string';
  if (prop.type) return prop.type;
  if (prop.anyOf) {
    const t = prop.anyOf.find(x => x.type && x.type !== 'null');
    if (t) return t.type;
  }
  return 'string';
}

function defaultFor(prop) {
  const t = typeOf(prop);
  if (t === 'integer' || t === 'number') return 0;
  if (t === 'boolean') return false;
  return '';
}

function smartPrefill(actionName) {
  const act = (S.meta.actions || []).find(a => a.name === actionName);
  const props = (act && act.params && act.params.properties) || {};
  const sample = ((S.inst && S.inst.sample_action_params) || {})[actionName] || {};
  const out = {};
  Object.keys(props).forEach(k => {
    if (sample[k] !== undefined) { out[k] = sample[k]; return; }
    let found = null;
    for (const type of Object.keys(S.objects)) {
      const row = (S.objects[type] || []).find(o => o[k] !== undefined && o[k] !== '***MASKED***');
      if (row) { found = row[k]; break; }
    }
    out[k] = found !== null ? found : defaultFor(props[k]);
  });
  return out;
}

function fillParams() {
  const act = currentAction();
  if (!act) { $('form').innerHTML = ''; $('actmeta').textContent = ''; return; }
  S.lastAction = act.name;
  const props = (act.params && act.params.properties) || {};
  const values = smartPrefill(act.name);
  $('actmeta').textContent = '可执行角色：' + ((act.allowed_roles || []).map(zhRole).join(' / ') || '—');
  $('form').innerHTML = Object.keys(props).map(k => {
    const t = typeOf(props[k]);
    const v = values[k];
    const head = '<span class="fl">' + esc(zhField(null, k)) + '<em>' + esc(k) + '</em></span>';
    if (t === 'boolean') {
      return '<label class="f">' + head + '<select id="p_' + esc(k) + '"><option value="true"' +
        (v ? ' selected' : '') + '>是</option><option value="false"' + (v ? '' : ' selected') + '>否</option></select></label>';
    }
    return '<label class="f">' + head + '<input id="p_' + esc(k) + '" type="' +
      (t === 'integer' || t === 'number' ? 'number' : 'text') + '" value="' + esc(v) + '"></label>';
  }).join('');
}

function collectParams() {
  const act = currentAction();
  const props = (act && act.params && act.params.properties) || {};
  const out = {};
  Object.keys(props).forEach(k => {
    const el = $('p_' + k);
    if (!el) return;
    const t = typeOf(props[k]);
    if (t === 'integer') out[k] = parseInt(el.value || '0', 10);
    else if (t === 'number') out[k] = parseFloat(el.value || '0');
    else if (t === 'boolean') out[k] = el.value === 'true';
    else out[k] = el.value;
  });
  return out;
}

function pickObject(idx) {
  const o = (S.objects[S.type] || [])[idx];
  if (!o) return;
  const act = currentAction();
  const props = (act && act.params && act.params.properties) || {};
  Object.keys(props).forEach(k => {
    const el = $('p_' + k);
    if (!el) return;
    if (o[k] !== undefined && o[k] !== '***MASKED***') el.value = o[k];
  });
  toast('已载入对象参数');
}

async function quickSim(idx) { pickObject(idx); await simulate(); }

function show(title, payload) {
  const el = $('result');
  if (el) el.textContent = title + '\n' + JSON.stringify(payload, null, 2);
}

function diffTable(diff) {
  const changes = (diff && diff.object_changes) || [];
  if (!changes.length) return '<div class="hint">无对象变更。</div>';
  return '<table class="diff"><tr><th>变更</th><th>对象</th><th>变更前</th><th>变更后</th></tr>' +
    changes.map(c => '<tr><td>' + esc(c.kind) + '</td><td>' + esc(zhObject(c.object_type)) + '：' +
      esc(c.object_id) + '</td><td class="b">' + esc(JSON.stringify(c.before || {})) +
      '</td><td class="a">' + esc(JSON.stringify(c.after || {})) + '</td></tr>').join('') + '</table>';
}

async function simulate() {
  try {
    const data = await api('/api/simulate', {action: $('action').value, params: collectParams()});
    const el = $('result');
    if (el) el.innerHTML = '<b class="' + (data.valid ? 'ok' : 'err') + '">沙盘预演 → ' +
      (data.valid ? '通过' : '被拒绝 ' + esc(data.error_code || '')) + '</b>' +
      (data.error ? '<div class="err">' + esc(data.error) + '</div>' : '') +
      '<div class="hint">需要审批：' + (data.requires_approval ? '是' : '否') + '</div>' + diffTable(data.diff);
    toast(data.valid ? '沙盘预演通过（主库零副作用）' : '沙盘预演被符号裁判拒绝', !data.valid);
  } catch (e) { show('沙盘预演失败', {error: e.message}); toast(e.message, true); }
}

async function submitAction() {
  try {
    const runId = 'CC-' + Date.now();
    const data = await api('/api/submit', {run_id: runId, action: $('action').value, params: collectParams()});
    const el = $('result');
    if (el) el.innerHTML = '<b class="ok">正式执行 → ' + esc(zhStatus(data.run.status)) + '</b>' +
      '<div class="hint">执行编号：' + esc(data.run.run_id) + '</div>' + diffTable(data.run.object_diff);
    toast('已正式执行：' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { show('正式执行失败', {error: e.message}); toast(e.message, true); }
}

async function refreshInstance() {
  if (!S.inst) return;
  try {
    S.catalog = await api('/api/catalog');
    const fresh = S.catalog.instances.find(i => i.instance_id === S.inst.instance_id);
    if (fresh) S.inst = fresh;
    chips();
    renderLists();
    const fp = await api('/api/fingerprint');
    const runs = (await api('/api/action-runs')).runs;
    const links = (await api('/api/links')).links;
    for (const t of S.meta.object_types) {
      const res = await api('/api/objects/' + encodeURIComponent(t.name));
      S.objects[t.name] = res.objects;
    }
    renderFingerprint(fp);
    renderObjects();
    renderLinks(links);
    renderRuns(runs);
  } catch (e) { toast(e.message, true); }
}

async function runMcts() {
  try {
    const box = $('mcts');
    if (box) box.innerHTML = '多步智能推演中……（平行沙盘，主库零污染）';
    const data = await api('/api/instance/mcts', {num_simulations: 80});
    S.mcts = data;
    const rows = (data.root_stats || []).map(r => '<tr><td><b>' + esc(r.label) + '</b></td><td>' +
      Number(r.prior).toFixed(3) + '</td><td>' + r.visits + '</td><td>' + Number(r.mean_value).toFixed(0) + '</td></tr>').join('');
    const pruned = (data.pruned_branches || []).map(p => '<span class="badge r">' + esc(p.action || JSON.stringify(p)) + '</span>').join('') ||
      '<span class="hint">无</span>';
    if (box) box.innerHTML = '<div class="sect">🌳 多步智能推演 · 根节点统计 P / N / Q</div>' +
      '<table><tr><th>业务动作</th><th>P</th><th>N</th><th>Q</th></tr>' + rows + '</table>' +
      '<div class="sect">符号裁判剪枝</div><div class="badges">' + pruned + '</div>' +
      '<div class="sect">最优序列 · 终局价值 ' + Number(data.best_value).toFixed(0) + '</div>' +
      '<div class="badges">' + (data.best_sequence || []).map((s, i) =>
        '<span class="badge c">' + (i + 1) + '. ' + esc(s) + '</span>').join('') + '</div>' +
      '<button class="g" style="margin-top:8px" onclick="commitMcts()">⚡ 一键提交最优序列</button>';
    toast('多步推演完成：终局价值 ' + data.best_value);
  } catch (e) {
    const box = $('mcts');
    if (box) box.innerHTML = '<span class="err">多步推演失败：' + esc(e.message) + '</span>';
    toast(e.message, true);
  }
}

async function commitMcts() {
  try {
    const data = await api('/api/instance/mcts', {num_simulations: 80, commit: true});
    toast('最优序列已正式执行（' + (data.committed_runs || []).length + ' 步）');
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function approveRun(runId) {
  try {
    const data = await api('/api/approve', {run_id: runId});
    toast('已批准 ' + runId + ' → ' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function revertRun(runId) {
  try {
    const data = await api('/api/revert', {run_id: runId});
    toast('已撤回 ' + runId + ' → ' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function runAutomations() {
  try {
    const data = await api('/api/automations/run', {});
    toast(data.fired.length ? ('自动化规则触发 ' + data.fired.length + ' 次') : '没有新的可触发对象');
    if (S.inst) await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function forkInstance() {
  if (!S.inst) { toast('请先选择一个运行实例', true); return; }
  try {
    const data = await api('/api/instance/fork', {instance: S.inst.instance_id, label: S.inst.label + ' · 克隆沙盘'});
    S.catalog = await api('/api/catalog');
    chips();
    renderLists();
    toast('已克隆沙盘：' + data.instance.instance_id);
    await selectInstance(data.instance.instance_id);
  } catch (e) { toast(e.message, true); }
}

async function unplug() {
  if (!S.inst) return;
  try {
    const data = await api('/api/instance/unplug', {instance: S.inst.instance_id});
    toast('已断开外部系统：' + (data.unplugged || []).join(', '));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

function renderRuns(runs) {
  const box = $('runs');
  if (!box) return;
  if (!runs.length) { box.innerHTML = '<div class="hint">动作执行记录为空。</div>'; return; }
  box.innerHTML = runs.slice().reverse().map(r => {
    const ops = [];
    if (r.status === 'PENDING_APPROVAL') ops.push('<button class="g" onclick="approveRun(\'' + esc(r.run_id) + '\')">✅ 批准</button>');
    if (r.status === 'COMMITTED') ops.push('<button class="r" onclick="revertRun(\'' + esc(r.run_id) + '\')">↩️ 撤回</button>');
    return '<div class="run"><div class="id">' + esc(r.run_id) + ' <span class="tag t-' + esc(r.status) + '">' +
      esc(zhStatus(r.status)) + '</span></div><div class="kv"><span>' + esc(zhAction(r.action_type)) + '</span><span>' +
      esc(zhRole(r.actor_role)) + '</span><span class="err">' + esc(r.error_code || '') + '</span></div>' +
      (ops.length ? '<div class="ops">' + ops.join('') + '</div>' : '') + '</div>';
  }).join('');
}

loadAll();
</script></body></html>
"""


def _render_ui_page(template: str, title: str) -> str:
    """注入共享中文展示助手、拓扑渲染器与页面标题。

    展示映射与渲染脚本都是**纯展示层**资源，不会改写任何 API 标识、
    角色 value 或动作参数。
    """
    bundle = DISPLAY_JS.replace("__DISPLAY_JSON__", display_json())
    return (
        template.replace("__UI_BUNDLE__", bundle)
        .replace("__TOPOLOGY_JS__", TOPOLOGY_JS)
        .replace("__TITLE__", title)
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _WorkshopHandler(BaseHTTPRequestHandler):
    server_version = "OpenOntologyCommandCenter/0.5"
    protocol_version = "HTTP/1.1"

    # -- server accessors --------------------------------------------------
    @property
    def _registry(self) -> CommandCenterRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    @property
    def _lock(self) -> threading.RLock:
        return self.server.runtime_lock  # type: ignore[attr-defined]

    @property
    def _title(self) -> str:
        return self.server.title  # type: ignore[attr-defined]

    def _instance_entry(self, body: dict[str, Any] | None, query: dict[str, list[str]] | None) -> OntologyInstanceEntry:
        instance_id = None
        if body:
            instance_id = body.get("instance")
        if not instance_id and query:
            instance_id = query.get("instance", [None])[0]
        if not instance_id:
            return self._registry.default_instance()
        return self._registry.get_instance(str(instance_id))

    def _runtime(self, body: dict[str, Any] | None = None, query: dict[str, list[str]] | None = None) -> OntologyRuntime:
        return self._instance_entry(body, query).runtime

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _actor(self, role: str | None = None, entry: OntologyInstanceEntry | None = None) -> Actor:
        if role:
            return Actor(actor_id=f"workshop:{role}", role=role, clearance_tags={"*"})
        if entry is not None and entry.default_actor is not None:
            return entry.default_actor
        role = self.server.default_actor.role  # type: ignore[attr-defined]
        return Actor(actor_id=f"workshop:{role}", role=role, clearance_tags={"*"})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    # -- meta / catalog ----------------------------------------------------
    def _meta(self, actor: Actor, runtime: OntologyRuntime, entry: OntologyInstanceEntry) -> dict[str, Any]:
        payload = _spec_meta(runtime.spec)
        payload.update(
            {
                "ok": True,
                "actor": {"actor_id": actor.actor_id, "role": actor.role},
                "instance": _instance_summary(entry, self._registry),
                "counts": {
                    "objects": runtime.count_objects(),
                    "runs": len(runtime.list_action_runs()),
                },
            }
        )
        return payload

    def _catalog(self) -> dict[str, Any]:
        ontologies = []
        for entry in self._registry.ontologies.values():
            meta = _spec_meta(entry.spec)
            instances = [
                item.instance_id for item in self._registry.instances_for(entry.ontology_id)
            ]
            ontologies.append(
                {
                    "ontology_id": entry.ontology_id,
                    "name": entry.name,
                    "category": entry.category,
                    "description": entry.description,
                    "counts": {
                        "object_types": len(meta["object_types"]),
                        "interfaces": len(meta["interfaces"]),
                        "link_types": len(meta["link_types"]),
                        "functions": len(meta["functions"]),
                        "actions": len(meta["actions"]),
                        "automations": len(meta["automations"]),
                        "instances": len(instances),
                    },
                    "instances": instances,
                }
            )
        instances = [
            _instance_summary(entry, self._registry)
            for entry in self._registry.instances.values()
        ]
        return {"ok": True, "ontologies": ontologies, "instances": instances}

    def _ontology_detail(self, ontology_id: str) -> dict[str, Any]:
        entry = self._registry.get_ontology(ontology_id)
        payload = _spec_meta(entry.spec)
        payload.update(
            {
                "ok": True,
                "ontology": {
                    "ontology_id": entry.ontology_id,
                    "name": entry.name,
                    "category": entry.category,
                    "description": entry.description,
                },
                "counts": {
                    "object_types": len(payload["object_types"]),
                    "interfaces": len(payload["interfaces"]),
                    "link_types": len(payload["link_types"]),
                    "functions": len(payload["functions"]),
                    "actions": len(payload["actions"]),
                    "automations": len(payload["automations"]),
                    "instances": len(self._registry.instances_for(entry.ontology_id)),
                },
                "instances": [
                    _instance_summary(item, self._registry)
                    for item in self._registry.instances_for(entry.ontology_id)
                ],
            }
        )
        return payload

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        role = query.get("role", [None])[0]

        if path == "/":
            self._send(
                200,
                _render_ui_page(_WORKSHOP_HTML, self._title).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        if path == "/topology":
            # 独立、可刷新的矢量关系图页面：不读取也不回显 ontology 查询参数，
            # 前端再按 ?ontology=<编号> 请求 /api/ontology/<编号>。
            self._send(
                200,
                _render_ui_page(TOPOLOGY_PAGE_HTML, self._title).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        with self._lock:
            try:
                if path == "/api/health":
                    self._send_json(200, {"ok": True})
                    return
                if path == "/api/catalog":
                    self._send_json(200, self._catalog())
                    return

                if path.startswith("/api/ontology/"):
                    ontology_id = path[len("/api/ontology/"):]
                    self._send_json(200, self._ontology_detail(ontology_id))
                    return

                entry = self._instance_entry(None, query)
                runtime = entry.runtime
                actor = self._actor(role, entry)

                if path == "/api/meta":
                    self._send_json(200, self._meta(actor, runtime, entry))
                    return
                if path == "/api/fingerprint":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            **runtime.state_fingerprint(),
                        },
                    )
                    return
                if path in ("/api/action-runs", "/api/runs"):
                    status = query.get("status", [None])[0]
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            "runs": runtime.list_action_runs(status),
                        },
                    )
                    return
                if path == "/api/automations/history":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "history": runtime.automation_history(),
                        },
                    )
                    return
                if path == "/api/links":
                    rows = runtime.connection.execute(
                        """
                        SELECT link_type, source_type, source_id, target_type, target_id
                        FROM ontology_links ORDER BY link_type, source_id, target_id
                        """
                    ).fetchall()
                    links = [dict(row) for row in rows]
                    self._send_json(
                        200,
                        {"ok": True, "count": len(links), "links": links},
                    )
                    return
                if path == "/api/interface":
                    name = query.get("interface", [None])[0]
                    if not name:
                        raise ValueError("interface query parameter is required")
                    objects = runtime.query_interface(name, actor)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "interface": name,
                            "count": len(objects),
                            "objects": [o.model_dump(mode="json") for o in objects],
                        },
                    )
                    return

                prefix = "/api/objects/"
                if path.startswith(prefix):
                    object_type = path[len(prefix):]
                    if not object_type or "/" in object_type:
                        self._error(404, "object type required")
                        return
                    cls = runtime.spec.get_object_type(object_type)
                    objects = runtime.query_objects(cls, actor)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "object_type": cls.__name__,
                            "count": len(objects),
                            "objects": [o.model_dump(mode="json") for o in objects],
                        },
                    )
                    return

                self._error(404, "not found")
            except KeyError as exc:
                self._error(404, str(exc))
            except OpenOntologyError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        role = query.get("role", [None])[0]

        try:
            body = self._read_json()
        except ValueError as exc:
            self._error(400, str(exc))
            return

        with self._lock:
            try:
                entry = self._instance_entry(body, query)
                actor = self._actor(body.get("role") or role, entry)
                self._dispatch_post(path, body, actor, entry)
            except KeyError as exc:
                self._error(404, str(exc))
            except OpenOntologyError as exc:
                self._error(400, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    def _dispatch_post(
        self,
        path: str,
        body: dict[str, Any],
        actor: Actor,
        entry: OntologyInstanceEntry,
    ) -> None:
        runtime = entry.runtime

        if path == "/api/simulate":
            sim = runtime.simulate_action(
                body["action"], body.get("params") or {}, actor
            )
            self._send_json(200, {"ok": True, **sim.to_dict()})
            return

        if path == "/api/submit":
            run = runtime.submit_action(
                body.get("run_id") or f"UI-{len(runtime.list_action_runs()) + 1:04d}",
                body["action"],
                body.get("params") or {},
                actor,
            )
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/approve":
            run = runtime.approve_action(body["run_id"], actor)
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/revert":
            revert_run_id = body.get("revert_run_id") or f"{body['run_id']}-REV"
            run = runtime.revert_action(body["run_id"], revert_run_id, actor)
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/function":
            result = runtime.call_function(
                body["function"], body.get("params") or {}, actor
            )
            self._send_json(200, {"ok": True, "result": result})
            return

        if path == "/api/automations/run":
            fired = runtime.evaluate_automations(actor)
            self._send_json(
                200,
                {
                    "ok": True,
                    "fired": fired,
                    "count": len(fired),
                    "history": runtime.automation_history(),
                },
            )
            return

        if path == "/api/snapshot":
            objects = body.get("objects")
            if not isinstance(objects, dict):
                raise ValueError("snapshot.objects must be a JSON object")
            summary = runtime.load_snapshot(
                objects=objects,
                links=body.get("links"),
                conflict_strategy=str(
                    body.get("conflict_strategy", "user_edits_win")
                ),
                source_timestamps={
                    tuple(key.split(":", 1)): value
                    for key, value in (body.get("source_timestamps") or {}).items()
                }
                or None,
            )
            self._send_json(200, {"ok": True, **summary})
            return

        if path == "/api/instance/fork":
            forked = self._registry.fork_instance(
                body.get("instance") or entry.instance_id,
                body.get("new_instance_id"),
                body.get("label"),
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "instance": _instance_summary(forked, self._registry),
                },
            )
            return

        if path == "/api/instance/mcts":
            self._run_mcts(body, actor, entry)
            return

        if path == "/api/instance/unplug":
            self._unplug(body, entry)
            return

        self._error(404, "not found")

    # -- advanced instance operations --------------------------------------
    def _run_mcts(
        self, body: dict[str, Any], actor: Actor, entry: OntologyInstanceEntry
    ) -> None:
        config = dict(entry.mcts_config or {})
        if not config:
            raise ValueError(f"instance {entry.instance_id!r} has no MCTS config")
        if body.get("num_simulations") is not None:
            config["simulations"] = int(body["num_simulations"])
        if body.get("simulations") is not None:
            config["simulations"] = int(body["simulations"])
        for key in ("c_puct", "seed", "value_scale", "max_steps"):
            if body.get(key) is not None:
                config[key] = body[key]

        # 规划与提交都使用实例预设的默认角色（各蓝图角色体系不同）。
        planner_actor = entry.default_actor or actor
        report = entry.runtime.plan_mcts(planner_actor, **config)
        payload = {
            "ok": True,
            "instance": entry.instance_id,
            "root_stats": [
                {
                    "label": stat.label,
                    "action": stat.action,
                    "prior": stat.prior,
                    "visits": stat.visits,
                    "mean_value": stat.mean_value,
                }
                for stat in report.root_actions
            ],
            "best_sequence": report.best_plan_labels,
            "best_value": report.best_value,
            "pruned_branches": list(report.pruned_errors),
            "pruned_illegal_nodes": report.pruned_illegal_nodes,
            "simulations": report.simulations,
            "forks_created": report.forks_created,
            "sandbox_writes": report.sandbox_writes,
            "report": report.to_dict(),
        }
        if body.get("commit"):
            committed: list[str] = []
            for index, candidate in enumerate(report.best_plan, start=1):
                run_id = f"CC-MCTS-{entry.instance_id}-{index}-{uuid.uuid4().hex[:8]}"
                entry.runtime.submit_action(
                    run_id, candidate.action, candidate.params, planner_actor
                )
                committed.append(run_id)
            payload["committed_runs"] = committed
        self._send_json(200, payload)

    def _unplug(self, body: dict[str, Any], entry: OntologyInstanceEntry) -> None:
        if entry.unplug_fn is None:
            raise ValueError(f"instance {entry.instance_id!r} does not support unplug")
        result = entry.unplug_fn()
        for connector in entry.connectors:
            connector.status = "UNPLUGGED"
        self._send_json(
            200,
            {
                "ok": True,
                "instance": entry.instance_id,
                "unplugged": [connector.name for connector in entry.connectors],
                "connectors": [asdict(connector) for connector in entry.connectors],
                "result": result,
            },
        )

    # -- noise control -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        return


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------
class WorkshopServer(ThreadingHTTPServer):
    """线程化指挥中心服务器：一个注册表 + 一把请求锁。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runtime: OntologyRuntime | None = None,
        *,
        registry: CommandCenterRegistry | None = None,
        actor: Actor | None = None,
        title: str = "OpenOntology Workshop",
    ) -> None:
        if registry is None:
            if runtime is None:
                raise TypeError(
                    "WorkshopServer requires either a runtime or a registry"
                )
            registry = CommandCenterRegistry.from_runtime(
                runtime, actor=actor, title=title
            )
        self.registry = registry
        # 向后兼容属性：始终指向默认实例的 runtime。
        self.runtime = registry.default_instance().runtime
        self.runtime_lock = threading.RLock()
        self.title = title
        self.default_actor = actor or Actor(
            actor_id="workshop:admin", role="admin", clearance_tags={"*"}
        )
        super().__init__(server_address, _WorkshopHandler)


@dataclass
class WorkshopHandle:
    server: WorkshopServer
    thread: threading.Thread
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)


def _serve(server: WorkshopServer, host: str, port: int) -> WorkshopHandle:
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"open-ontology-workshop-{actual_port}",
        daemon=True,
    )
    thread.start()
    return WorkshopHandle(
        server=server,
        thread=thread,
        host=str(actual_host),
        port=int(actual_port),
    )


def start_workshop_server(
    runtime: OntologyRuntime | CommandCenterRegistry,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    actor: Actor | None = None,
    title: str = "OpenOntology Workshop",
    registry: CommandCenterRegistry | None = None,
) -> WorkshopHandle:
    """启动作战台。

    ``runtime`` 可以传单个 :class:`OntologyRuntime`（旧签名），也可以直接传
    :class:`CommandCenterRegistry`（新用法），或使用 ``registry=`` 关键字。
    """
    if isinstance(runtime, CommandCenterRegistry):
        registry = runtime
        runtime = None
    server = WorkshopServer(
        (host, port), runtime, registry=registry, actor=actor, title=title
    )
    return _serve(server, host, port)


def start_command_center_server(
    registry: CommandCenterRegistry,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    actor: Actor | None = None,
    title: str = "OpenOntology Command Center",
) -> WorkshopHandle:
    """多蓝图 / 多实例指挥中心的启动入口。"""
    server = WorkshopServer(
        (host, port), registry=registry, actor=actor, title=title
    )
    return _serve(server, host, port)
