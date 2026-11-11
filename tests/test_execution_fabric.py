from dataclasses import FrozenInstanceError
from pathlib import Path
import threading

import pytest

from open_ontology.execution.fabric import CallContext, ExecutionFabric, digest


def context(instance="north"):
    return CallContext(instance_id=instance, operation_id="OP-1", actor_id="USER-1")


def test_binding_unknown_and_duplicate():
    fabric = ExecutionFabric()
    with pytest.raises(KeyError):
        fabric.invoke("stock.lookup", {}, context())
    fabric.bind("stock.lookup", lambda p: {"site": "north"})
    with pytest.raises(ValueError):
        fabric.bind("stock.lookup", lambda p: {"site": "other"})


def test_instance_bindings_are_separate():
    north, south = ExecutionFabric(), ExecutionFabric()
    north.bind("stock.lookup", lambda p: {"site": "north"})
    south.bind("stock.lookup", lambda p: {"site": "south"})
    assert north.invoke("stock.lookup", {}, context("north")) == {"site": "north"}
    assert south.invoke("stock.lookup", {}, context("south")) == {"site": "south"}


def test_observer_does_not_change_payload_result_or_context():
    seen = []
    original_result = {"stock": 7}
    payload = {"sku": "SKU-1", "nested": {"n": 1}}
    received = []

    def tool(p):
        received.append(p)
        p["nested"]["n"] = 2
        return original_result

    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", tool)
    ctx = context()
    result = fabric.invoke("stock.lookup", payload, ctx)
    assert result is original_result
    assert payload == {"sku": "SKU-1", "nested": {"n": 1}}
    assert set(received[0]) == {"sku", "nested"}
    assert [e.phase for e in seen] == ["started", "succeeded"]
    assert all(e.context is ctx for e in seen)
    assert not hasattr(seen[0], "payload")
    with pytest.raises(FrozenInstanceError):
        seen[0].phase = "tampered"


def test_tool_failure_is_not_wrapped_as_success():
    seen = []
    failure = RuntimeError("tool failed")

    def tool(p):
        raise failure

    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", tool)
    with pytest.raises(RuntimeError) as exc:
        fabric.invoke("stock.lookup", {}, context())
    assert exc.value is failure
    assert [e.phase for e in seen] == ["started", "failed"]
    assert seen[-1].error_type == "RuntimeError"


def test_optional_observer_failure_preserves_success():
    def broken(event):
        raise RuntimeError("optional observer failed")

    fabric = ExecutionFabric(observers=(broken,))
    fabric.bind("stock.lookup", lambda p: {"stock": 7})
    assert fabric.invoke("stock.lookup", {}, context()) == {"stock": 7}


def test_observers_on_and_off_are_equivalent():
    calls = {"observed": 0, "silent": 0}

    def echo(p):
        return {"echo": p["sku"], "n": p["nested"]["n"]}

    def failing_tool(p):
        raise LookupError("missing stock")

    def observed_tool(p):
        calls["observed"] += 1
        return echo(p)

    def silent_tool(p):
        calls["silent"] += 1
        return echo(p)

    payload = {"sku": "SKU-1", "nested": {"n": 1}}

    observed = ExecutionFabric(observers=(lambda e: None,))
    observed.bind("stock.lookup", observed_tool)
    observed.bind("stock.fail", failing_tool)

    silent = ExecutionFabric()
    silent.bind("stock.lookup", silent_tool)
    silent.bind("stock.fail", failing_tool)

    assert observed.invoke("stock.lookup", payload, context()) == silent.invoke(
        "stock.lookup", payload, context()
    )
    with pytest.raises(LookupError) as on_exc:
        observed.invoke("stock.fail", payload, context())
    with pytest.raises(LookupError) as off_exc:
        silent.invoke("stock.fail", payload, context())

    assert str(on_exc.value) == str(off_exc.value)
    assert calls == {"observed": 1, "silent": 1}


def test_concurrent_instances_keep_their_own_context():
    barrier = threading.Barrier(4)
    results = []

    def make_fabric(instance):
        seen = []

        def tool(p):
            barrier.wait(timeout=5)
            return {"instance": instance, "op": p["op"]}

        fabric = ExecutionFabric(observers=(seen.append,))
        fabric.bind("stock.lookup", tool)
        return fabric, seen

    def run(instance, op):
        fabric, seen = make_fabric(instance)
        result = fabric.invoke(
            "stock.lookup",
            {"op": op},
            CallContext(instance_id=instance, operation_id=op, actor_id="USER-1"),
        )
        results.append((instance, op, result, seen))

    threads = [
        threading.Thread(target=run, args=("north", "OP-1")),
        threading.Thread(target=run, args=("north", "OP-2")),
        threading.Thread(target=run, args=("south", "OP-1")),
        threading.Thread(target=run, args=("south", "OP-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 4
    for instance, op, result, seen in results:
        assert result == {"instance": instance, "op": op}
        assert [e.phase for e in seen] == ["started", "succeeded"]
        for event in seen:
            assert event.context.instance_id == instance
            assert event.context.operation_id == op


def test_invalid_input_fails_before_tool_runs():
    calls = []
    seen = []
    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", lambda p: calls.append(p) or {"ok": True})

    circular = {}
    circular["self"] = circular

    for bad in ({"value": object()}, {"value": float("nan")}, circular):
        with pytest.raises((TypeError, ValueError)):
            fabric.invoke("stock.lookup", bad, context())

    assert calls == []
    assert seen == []


def test_non_json_result_is_preserved_and_still_succeeds():
    sentinel = object()
    seen = []
    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", lambda p: sentinel)

    assert fabric.invoke("stock.lookup", {}, context()) is sentinel
    assert [e.phase for e in seen] == ["started", "succeeded"]
    assert seen[-1].output_digest is None
    assert seen[-1].error_type is None


def test_no_implicit_retry():
    calls = []
    fabric = ExecutionFabric(observers=(lambda e: None,))
    fabric.bind("stock.lookup", lambda p: calls.append(p) or {"ok": True})
    fabric.invoke("stock.lookup", {}, context())
    assert len(calls) == 1

    failures = []

    def failing(p):
        failures.append(p)
        raise RuntimeError("boom")

    fabric.bind("stock.fail", failing)
    with pytest.raises(RuntimeError):
        fabric.invoke("stock.fail", {}, context())
    assert len(failures) == 1


def test_rejection_happens_before_any_observation():
    seen = []
    fabric = ExecutionFabric(observers=(seen.append,))
    with pytest.raises(KeyError):
        fabric.invoke("stock.unknown", {}, context())
    fabric.bind("stock.lookup", lambda p: {"ok": True})
    with pytest.raises(ValueError):
        fabric.bind("stock.lookup", lambda p: {"ok": False})
    assert seen == []


def test_tool_receives_no_injected_metadata():
    payload = {"sku": "SKU-1", "nested": {"n": 1}}
    received = []
    fabric = ExecutionFabric(observers=(lambda e: None,))
    fabric.bind("stock.lookup", lambda p: received.append(p) or {"ok": True})
    fabric.invoke("stock.lookup", payload, context())
    assert received[0] == {"sku": "SKU-1", "nested": {"n": 1}}
    assert received[0] is not payload


def test_business_modules_do_not_import_execution():
    root = Path(__file__).resolve().parents[1]
    candidates = sorted((root / "open_ontology").glob("*.py")) + sorted(
        (root / "examples").glob("*.py")
    )
    assert candidates
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        assert "open_ontology.execution" not in text, path
        assert "from .execution" not in text, path


def test_broken_observer_does_not_replace_tool_exception():
    def broken(event):
        raise RuntimeError("optional observer failed")

    failure = LookupError("missing stock")
    fabric = ExecutionFabric(observers=(broken, broken))
    fabric.bind("stock.lookup", lambda p: (_ for _ in ()).throw(failure))
    with pytest.raises(LookupError) as exc:
        fabric.invoke("stock.lookup", {}, context())
    assert exc.value is failure


def test_remaining_observers_still_receive_records_in_order():
    def broken(event):
        raise RuntimeError("optional observer failed")

    seen = []
    fabric = ExecutionFabric(observers=(broken, seen.append))
    fabric.bind("stock.lookup", lambda p: {"stock": 7})
    assert fabric.invoke("stock.lookup", {}, context()) == {"stock": 7}
    assert [e.phase for e in seen] == ["started", "succeeded"]


def test_context_keeps_identity_mode_and_attempt():
    seen = []
    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", lambda p: {"stock": 7})
    ctx = CallContext(
        instance_id="north",
        operation_id="OP-9",
        actor_id="USER-1",
        mode="simulate",
        attempt=3,
    )
    fabric.invoke("stock.lookup", {}, ctx)
    assert all(e.context is ctx for e in seen)
    assert seen[0].context.mode == "simulate"
    assert seen[0].context.attempt == 3
    with pytest.raises(FrozenInstanceError):
        ctx.mode = "live"


def test_digest_ignores_key_order():
    assert digest({"a": 1, "b": [2, 3]}) == digest({"b": [2, 3], "a": 1})


@pytest.mark.parametrize("value", [(1, 2), {1: "same"}, {"nested": (1, 2)}])
def test_digest_rejects_values_outside_plain_json(value):
    with pytest.raises((TypeError, ValueError)):
        digest(value)


@pytest.mark.parametrize("params", [{"nested": (1, 2)}, {1: "same"}])
def test_non_json_request_is_rejected_before_execution(params):
    calls, records = [], []
    fabric = ExecutionFabric(observers=(records.append,))
    fabric.bind("probe", lambda p: calls.append(p) or {"ok": True})
    with pytest.raises((TypeError, ValueError)):
        fabric.invoke("probe", params, CallContext("north", "OP-JSON", "reviewer"))
    assert calls == []
    assert records == []


@pytest.mark.parametrize("output", [{"nested": (1, 2)}, {1: "same"}])
def test_non_json_output_is_preserved_without_misleading_digest(output):
    calls, records = [], []
    fabric = ExecutionFabric(observers=(records.append,))
    fabric.bind("probe", lambda p: calls.append(p) or output)
    result = fabric.invoke("probe", {}, CallContext("north", "OP-JSON", "reviewer"))
    assert result is output
    assert len(calls) == 1
    assert [e.phase for e in records] == ["started", "succeeded"]
    assert records[-1].output_digest is None


def test_plain_json_values_still_digest():
    value = {"s": "x", "i": 1, "f": 2.5, "b": True, "n": None, "l": [1, "a"]}
    assert isinstance(digest(value), str)
