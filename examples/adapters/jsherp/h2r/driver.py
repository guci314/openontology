from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from open_ontology.settlement import SourceBridge

_STD_ROLES = frozenset({
    "admin", "steward", "ai_agent", "ops", "sales_rep", "sales_manager",
    "support_agent", "warehouse", "buyer", "procurement_manager", "finance",
    "finance_director", "production_planner", "shopfloor", "plant_manager",
    "chief_accountant", "hr_admin", "hr_director", "system",
})
_EMP_STATUSES = frozenset({"onboarding", "active", "suspended", "offboarded"})


def _strip_prefix(val: Any, prefix: str) -> str:
    s = str(val or "")
    return s[len(prefix):] if prefix and s.startswith(prefix) else s


def _parse_int(val: Any) -> int | None:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


def build_device_bridge(
    endpoint_config: Mapping[str, Any],
    mapping: Any,
) -> SourceBridge:
    base_url = str(endpoint_config["base_url"]).rstrip("/")
    login_url = str(endpoint_config.get("login_url") or f"{base_url}/user/login")
    username = str(endpoint_config["username"])
    password_md5 = str(endpoint_config.get("password_md5") or "")
    if not password_md5:
        password_md5 = hashlib.md5(str(endpoint_config.get("password") or "").encode("utf-8")).hexdigest()
    token_header = str(endpoint_config.get("token_header") or "X-Access-Token")

    prefixes = {em.target_object_type: str(em.id_transform.prefix or "") for em in mapping.entity_mappings}
    ent_prefix = prefixes.get("AccountingEntity", "ENT-")
    dept_prefix = prefixes.get("Department", "DEPT-")
    pos_prefix = prefixes.get("JobPosition", "POS-")
    emp_prefix = prefixes.get("Employee", "EMP-")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    state: dict[str, Any] = {"token": None, "tenant_id": None, "last_write_ts": 0.0}

    def _login() -> str:
        payload = json.dumps({"loginName": username, "password": password_md5}).encode("utf-8")
        req = urllib.request.Request(login_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with opener.open(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or {}
        token = data.get("token")
        if not token:
            raise RuntimeError(f"Login failed: {body}")
        user_obj = data.get("user") or {}
        if user_obj.get("tenantId") is not None:
            state["tenant_id"] = int(user_obj["tenantId"])
        state["token"] = str(token)
        return state["token"]

    def _request(method: str, path: str, body: Mapping[str, Any] | None = None, is_write: bool = False) -> Any:
        if not state["token"]:
            _login()
        url = path if path.startswith("http") else f"{base_url}{path}"
        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            if is_write:
                elapsed = time.monotonic() - float(state["last_write_ts"])
                if elapsed < 1.05:
                    time.sleep(1.05 - elapsed)
            headers = {token_header: str(state["token"])}
            if data_bytes is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                with opener.open(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8").strip()
                if is_write:
                    state["last_write_ts"] = time.monotonic()
                parsed = json.loads(raw) if raw else {}
                if attempt == 0 and isinstance(parsed, dict) and parsed.get("code") in (401, 500, 600):
                    _login()
                    continue
                return parsed
            except urllib.error.HTTPError as exc:
                if is_write:
                    state["last_write_ts"] = time.monotonic()
                if attempt == 0 and exc.code in (401, 500):
                    _login()
                    continue
                raise
        return {}

    def _fetch_org_tree_nodes() -> list[tuple[int, int | None, str]]:
        tree = _request("GET", "/organization/getOrganizationTree?id=")
        nodes: list[tuple[int, int | None, str]] = []

        def _walk(items: Any, parent_id: int | None) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if isinstance(item, dict):
                    oid = _parse_int(item.get("id"))
                    if oid is not None:
                        nodes.append((oid, parent_id, str(item.get("title") or "")))
                        _walk(item.get("children"), oid)

        _walk(tree, None)
        return nodes

    def _fetch_rows(path: str) -> list[dict[str, Any]]:
        resp = _request("GET", path)
        rows = ((resp.get("data") or {}).get("rows")) if isinstance(resp, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def observe(move_envelope: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        _ = move_envelope
        sys_resp = _request("GET", "/systemConfig/getCurrentInfo")
        sys_data = sys_resp.get("data") if isinstance(sys_resp, dict) and isinstance(sys_resp.get("data"), dict) else {}

        tenant_id = _parse_int(sys_data.get("tenantId")) or state.get("tenant_id")
        if tenant_id is None:
            info_resp = _request("GET", "/user/infoWithTenant")
            info_data = info_resp.get("data") if isinstance(info_resp, dict) else {}
            tenant_id = _parse_int(info_data.get("tenantId")) if isinstance(info_data, dict) else None
        if tenant_id is None:
            _login()
            tenant_id = state.get("tenant_id")

        ent_id = f"{ent_prefix}{tenant_id}"
        accounting_entities = [{
            "entity_id": ent_id,
            "name": str(sys_data.get("companyName") or f"Tenant {tenant_id}"),
            "base_currency": "CNY",
            "status": "active",
        }]

        departments: list[dict[str, Any]] = []
        active_dept_ids: set[int] = set()
        for oid, tree_parent_id, tree_title in _fetch_org_tree_nodes():
            detail_resp = _request("GET", f"/organization/findById?id={oid}")
            detail = detail_resp.get("data") if isinstance(detail_resp, dict) else None
            if not isinstance(detail, dict) or not detail:
                continue
            raw_parent = _parse_int(detail.get("parentId"))
            if raw_parent is None:
                raw_parent = tree_parent_id
            active_dept_ids.add(oid)
            departments.append({
                "department_id": f"{dept_prefix}{oid}",
                "name": str(detail.get("orgAbr") or tree_title),
                "scope_tag": str(detail.get("remark") or ""),
                "status": "active",
                "accounting_entity": ent_id,
                "parent_department": f"{dept_prefix}{raw_parent}" if raw_parent is not None else None,
            })

        positions: list[dict[str, Any]] = []
        active_pos_ids: set[int] = set()
        for role in _fetch_rows("/role/list?currentPage=1&pageSize=200&search=%7B%7D"):
            if str(role.get("deleteFlag") or "0") == "1":
                continue
            rid = _parse_int(role.get("id"))
            desc = role.get("description")
            if rid is None or not isinstance(desc, str) or not desc.strip().startswith("{"):
                continue
            try:
                meta = json.loads(desc)
            except ValueError:
                continue
            if not isinstance(meta, dict):
                continue
            dept_raw = _parse_int(meta.get("department_id"))
            role_name = str(meta.get("role_name") or "").strip()
            if dept_raw is None or dept_raw not in active_dept_ids or role_name not in _STD_ROLES:
                continue
            ceiling = _parse_int(meta.get("approval_ceiling_cents")) or 0
            active_pos_ids.add(rid)
            positions.append({
                "position_id": f"{pos_prefix}{rid}",
                "title": str(role.get("name") or ""),
                "role_name": role_name,
                "approval_ceiling_cents": max(0, ceiling),
                "status": "active" if role.get("enabled", True) is not False else "inactive",
                "department": f"{dept_prefix}{dept_raw}",
            })

        employees: list[dict[str, Any]] = []
        for user in _fetch_rows("/user/list?currentPage=1&pageSize=200&search=%7B%7D"):
            login_name = str(user.get("loginName") or "")
            if not login_name or login_name == username or str(user.get("deleteFlag") or "0") == "1":
                continue
            uid = _parse_int(user.get("id"))
            raw_status = _parse_int(user.get("status"))
            orga_id = _parse_int(user.get("orgaId"))
            role_id = _parse_int(user.get("roleId"))
            if uid is None or raw_status == 1 or orga_id not in active_dept_ids or role_id not in active_pos_ids:
                continue
            if raw_status is not None and raw_status != 0:
                emp_status = "offboarded"
            else:
                desc_status = str(user.get("description") or "").strip().lower()
                emp_status = desc_status if desc_status in _EMP_STATUSES else "active"
            employees.append({
                "employee_id": f"{emp_prefix}{uid}",
                "full_name": str(user.get("username") or login_name),
                "actor_id": login_name,
                "status": emp_status,
                "department": f"{dept_prefix}{orga_id}",
                "position": f"{pos_prefix}{role_id}",
            })

        return {
            "AccountingEntity": accounting_entities,
            "Department": departments,
            "JobPosition": positions,
            "Employee": employees,
        }

    def actuate(move_envelope: Mapping[str, Any]) -> None:
        action = str(move_envelope.get("action_type") or "")
        params = move_envelope.get("params") or {}

        if action == "reparent_department":
            raw_dept_id = _parse_int(_strip_prefix(params.get("department_id"), dept_prefix))
            if raw_dept_id is None:
                return
            detail_resp = _request("GET", f"/organization/findById?id={raw_dept_id}")
            detail = detail_resp.get("data") if isinstance(detail_resp, dict) else None
            if not isinstance(detail, dict) or not detail.get("id"):
                return
            new_parent_str = str(params.get("new_parent_id") or "").strip()
            new_parent_id = _parse_int(_strip_prefix(new_parent_str, dept_prefix)) if new_parent_str else None
            payload = {
                "id": raw_dept_id,
                "orgAbr": detail.get("orgAbr"),
                "orgNo": detail.get("orgNo"),
                "sort": detail.get("sort"),
                "remark": detail.get("remark"),
                "parentId": new_parent_id,
            }
            try:
                _request("PUT", "/organization/update", body=payload, is_write=True)
            except urllib.error.HTTPError:
                return

        elif action == "assign_employee_position":
            raw_emp_id = _parse_int(_strip_prefix(params.get("employee_id"), emp_prefix))
            raw_dept_id = _parse_int(_strip_prefix(params.get("department_id"), dept_prefix))
            raw_pos_id = _parse_int(_strip_prefix(params.get("position_id"), pos_prefix))
            if raw_emp_id is None or raw_dept_id is None or raw_pos_id is None:
                return
            users = _fetch_rows("/user/list?currentPage=1&pageSize=200&search=%7B%7D")
            target = next((u for u in users if _parse_int(u.get("id")) == raw_emp_id), None)
            if not target or (_parse_int(target.get("status")) or 0) != 0:
                return
            roles = _fetch_rows("/role/list?currentPage=1&pageSize=200&search=%7B%7D")
            role_row = next((r for r in roles if _parse_int(r.get("id")) == raw_pos_id), None)
            activate = params.get("activate", True)
            desc = "active" if activate else str(target.get("description") or "onboarding")
            payload = {
                "id": raw_emp_id,
                "username": target.get("username"),
                "loginName": target.get("loginName"),
                "leaderFlag": target.get("leaderFlag") or "0",
                "position": role_row.get("name") if role_row else target.get("position"),
                "orgaId": raw_dept_id,
                "orgaUserRelId": target.get("orgaUserRelId"),
                "roleId": raw_pos_id,
                "email": target.get("email"),
                "phonenum": target.get("phonenum"),
                "description": desc,
                "remark": target.get("remark"),
            }
            try:
                _request("PUT", "/user/updateUser", body=payload, is_write=True)
            except urllib.error.HTTPError:
                return

        elif action == "offboard_employee":
            raw_emp_id = _parse_int(_strip_prefix(params.get("employee_id"), emp_prefix))
            if raw_emp_id is None:
                return
            users = _fetch_rows("/user/list?currentPage=1&pageSize=200&search=%7B%7D")
            if not any(_parse_int(u.get("id")) == raw_emp_id for u in users):
                return
            try:
                _request("POST", "/user/batchSetStatus", body={"status": 2, "ids": str(raw_emp_id)}, is_write=True)
            except urllib.error.HTTPError:
                return

    return SourceBridge(actuate=actuate, observe=observe)
