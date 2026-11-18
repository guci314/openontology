"""极简领域模型演示：单一「整数」对象 + 单一「加法(n)」动作。

运行::

    python3 -m examples.integer_add_demo --port 8766
"""

from __future__ import annotations

import argparse
import time
from typing import Sequence

from pydantic import BaseModel, Field

from open_ontology import (
    Actor,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    start_workshop_server,
)


class IntegerValue(ObjectType):
    """整数对象：仅维护一个数值属性。"""

    id: str
    value: int = 0


class AddParams(BaseModel):
    """加法动作参数：仅一个整数参数 n。"""

    n: int = Field(description="要累加的整数 n")


class CreateIntegerParams(BaseModel):
    """创建整数对象参数。"""

    id: str = Field(description="新整数对象编号，例如 INT-2")
    value: int = Field(default=0, description="初始整数值")


def build_integer_runtime() -> tuple[OntologyRuntime, Actor]:
    spec = OntologySpec()
    spec.display = {
        "objects": {"IntegerValue": "整数"},
        "actions": {"add": "加法", "create_integer": "创建整数"},
        "fields": {"id": "编号", "value": "整数值", "n": "增量 n"},
    }
    spec.add_object_type(IntegerValue)

    @spec.action(
        name="add",
        params=AddParams,
        allowed_roles=["admin", "ops", "ai_agent"],
        description="执行 整数 = 整数 + n",
    )
    def add_handler(ctx, params: AddParams) -> dict:
        objs = ctx.query(IntegerValue)
        if not objs:
            raise ValueError("未找到整数对象")
        target = objs[0]
        new_val = target.value + params.n
        ctx.modify(target, value=new_val)
        return {"id": target.id, "before": target.value, "after": new_val}

    @spec.action(
        name="create_integer",
        params=CreateIntegerParams,
        allowed_roles=["admin", "ops", "ai_agent"],
        description="创建一个新的整数对象",
    )
    def create_integer_handler(ctx, params: CreateIntegerParams) -> dict:
        created = IntegerValue(id=params.id, value=params.value)
        ctx.create(created)
        return {"id": created.id, "value": created.value}

    runtime = OntologyRuntime(spec, db_path=":memory:")
    admin = Actor(actor_id="demo:admin", role="admin", clearance_tags={"*"})
    runtime.put_object(IntegerValue(id="INT-1", value=10))
    return runtime, admin


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="极简整数加法领域模型演示")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(list(argv) if argv is not None else None)

    runtime, admin = build_integer_runtime()
    handle = start_workshop_server(
        runtime,
        host=args.host,
        port=args.port,
        actor=admin,
        title="极简整数加法 · AIP 演示",
    )
    print(f"Integer Add Demo ready → {handle.base_url}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        handle.stop()
        runtime.close()
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
