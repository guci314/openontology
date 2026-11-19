"""可插拔领域种子本体论原语（SeedOntology · 纯 M2，领域无关）。

在符号主义训练中，``SeedOntology`` 等价于神经网络训练里的「网络架构归纳偏置 + 领域预训练底座」：
* 显式限定符号搜索的假设空间（抽象上位原型、可继承状态不变量与跃迁公理、默认角色词表）；
* 任何具体领域（企业 ERP 业财、医疗临床、低空防卫、智能制造）只需传入各自的 ``SeedOntology``，
  即可复用同一套 ``compile_blueprint``、``SymbolicCrucible`` 与 ``OntologyBuilderAgent / EvolverAgent``，
  无需改写 M2 归纳内核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..spec import InvariantError, ObjectType, OntologySpec

__all__ = [
    "SeedArchetypeHint",
    "SeedOntology",
    "seed_from_spec",
]


@dataclass(frozen=True)
class SeedArchetypeHint:
    """Declarative metadata describing one abstract archetype provided by a SeedOntology."""

    name: str
    description: str = ""
    default_aggregate_root: bool = False
    default_append_only: bool = False
    supports_fsm: bool = False
    supports_balance_conservation: bool = False
    discovery_role_hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "default_aggregate_root": self.default_aggregate_root,
            "default_append_only": self.default_append_only,
            "supports_fsm": self.supports_fsm,
            "supports_balance_conservation": self.supports_balance_conservation,
            "discovery_role_hints": list(self.discovery_role_hints),
        }


@dataclass(frozen=True)
class SeedOntology:
    """First-class pluggable domain inductive bias (backbone) for symbolic ontology training."""

    seed_id: str
    domain_name: str
    description: str
    base_spec: OntologySpec
    archetypes: tuple[SeedArchetypeHint, ...] = ()
    default_archetype: str = "ObjectType"
    extra_prior_notes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provides_non_negative: bool = False
    fsm_archetypes: tuple[str, ...] = ()
    balance_conservation_archetypes: tuple[str, ...] = ()

    def archetype_names(self) -> tuple[str, ...]:
        """Return every allowed archetype name (always includes base 'ObjectType')."""
        names: list[str] = ["ObjectType"]
        for name in self.base_spec.abstract_types:
            if name not in names:
                names.append(name)
        for hint in self.archetypes:
            if hint.name not in names:
                names.append(hint.name)
        return tuple(names)

    def get_archetype_hint(self, archetype: str) -> SeedArchetypeHint | None:
        for hint in self.archetypes:
            if hint.name == archetype:
                return hint
        return None

    def covers_non_negative(self) -> bool:
        return bool(self.provides_non_negative)

    def covers_fsm_for(self, cls: type[ObjectType]) -> bool:
        for arch_name in self.fsm_archetypes:
            arch_cls = self.base_spec.abstract_types.get(arch_name)
            if arch_cls is not None and issubclass(cls, arch_cls):
                return True
        return False

    def covers_balance_conservation_for(self, cls: type[ObjectType]) -> bool:
        for arch_name in self.balance_conservation_archetypes:
            arch_cls = self.base_spec.abstract_types.get(arch_name)
            if arch_cls is not None and issubclass(cls, arch_cls):
                return True
        return False

    def default_aggregate_root_for(self, archetype: str, base_cls: type[ObjectType]) -> bool:
        hint = self.get_archetype_hint(archetype)
        if hint is not None and hint.default_aggregate_root:
            return True
        return bool(getattr(base_cls, "__aggregate_root__", False))

    def default_append_only_for(self, archetype: str, base_cls: type[ObjectType]) -> bool:
        hint = self.get_archetype_hint(archetype)
        if hint is not None and hint.default_append_only:
            return True
        return bool(getattr(base_cls, "__append_only__", False))

    def resolve_base_class(self, archetype: str) -> type[ObjectType]:
        """Resolve an archetype name to a concrete Python base class for model compilation.

        Raises ``InvariantError(code='UNKNOWN_SEED_ARCHETYPE')`` if the archetype is
        not declared by this SeedOntology.
        """
        if archetype == "ObjectType":
            return ObjectType
        cls = self.base_spec.abstract_types.get(archetype)
        if cls is not None:
            return cls
        raise InvariantError(
            f"archetype {archetype!r} is not provided by seed ontology {self.seed_id!r}; "
            f"allowed archetypes: {list(self.archetype_names())}",
            code="UNKNOWN_SEED_ARCHETYPE",
        )

    def inherited_invariant_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.base_spec.invariants.keys()))

    def inherited_transition_invariant_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.base_spec.transition_invariants.keys()))

    def prompt_summary(self) -> dict[str, Any]:
        """Serialize inductive bias summary for LLM Builder/Evolver system/user prompts."""
        return {
            "seed_id": self.seed_id,
            "domain_name": self.domain_name,
            "description": self.description,
            "allowed_archetypes": list(self.archetype_names()),
            "archetypes": [h.to_dict() for h in self.archetypes],
            "provides_non_negative": self.provides_non_negative,
            "fsm_archetypes": list(self.fsm_archetypes),
            "balance_conservation_archetypes": list(self.balance_conservation_archetypes),
            "inherited_state_invariants": [
                {
                    "name": inv.name,
                    "code": inv.code,
                    "description": inv.description,
                }
                for inv in self.base_spec.invariants.values()
            ],
            "inherited_transition_invariants": [
                {
                    "name": tinv.name,
                    "code": tinv.code,
                    "description": tinv.description,
                }
                for tinv in self.base_spec.transition_invariants.values()
            ],
            "inherited_roles": sorted(self.base_spec.roles.keys()),
            "prior_notes": list(self.extra_prior_notes),
        }


def seed_from_spec(
    *,
    seed_id: str,
    domain_name: str,
    base_spec: OntologySpec,
    description: str = "",
    archetype_role_hints: Mapping[str, Sequence[str]] | None = None,
    default_archetype: str = "ObjectType",
    extra_prior_notes: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
    provides_non_negative: bool | None = None,
    provides_fsm: bool | None = None,
    provides_balance_conservation: bool | None = None,
) -> SeedOntology:
    """Introspect any OntologySpec's abstract types & axioms into a pluggable SeedOntology."""
    role_hints = archetype_role_hints or {}
    hints: list[SeedArchetypeHint] = []
    for name, cls in base_spec.abstract_types.items():
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        supports_fsm = hasattr(cls, "__transitions__") or hasattr(cls, "__status_field__")
        supports_conservation = hasattr(cls, "__voucher_delta_rules__")
        hints.append(
            SeedArchetypeHint(
                name=name,
                description=doc,
                default_aggregate_root=bool(getattr(cls, "__aggregate_root__", False)),
                default_append_only=bool(getattr(cls, "__append_only__", False)),
                supports_fsm=supports_fsm,
                supports_balance_conservation=supports_conservation,
                discovery_role_hints=tuple(role_hints.get(name, ())),
            )
        )
    has_non_neg = (
        bool(provides_non_negative)
        if provides_non_negative is not None
        else (
            bool(base_spec.invariants)
            and any(hasattr(cls, "__non_negative_fields__") for cls in base_spec.abstract_types.values())
        )
    )
    has_fsm = bool(provides_fsm) if provides_fsm is not None else bool(base_spec.transition_invariants)
    has_conservation = (
        bool(provides_balance_conservation)
        if provides_balance_conservation is not None
        else bool(base_spec.transition_invariants)
    )
    return SeedOntology(
        seed_id=seed_id,
        domain_name=domain_name,
        description=description,
        base_spec=base_spec,
        archetypes=tuple(hints),
        default_archetype=default_archetype,
        extra_prior_notes=tuple(extra_prior_notes),
        metadata=dict(metadata or {}),
        provides_non_negative=has_non_neg,
        fsm_archetypes=tuple(h.name for h in hints if h.supports_fsm) if has_fsm else (),
        balance_conservation_archetypes=(
            tuple(h.name for h in hints if h.supports_balance_conservation) if has_conservation else ()
        ),
    )
