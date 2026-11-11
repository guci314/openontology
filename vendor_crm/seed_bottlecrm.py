"""Seed the REAL BottleCRM SQLite database through its own Django ORM."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "bottlecrm_src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bottlecrm_settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

if not settings.configured:
    django.setup()

from django.utils import timezone  # noqa: E402

from accounts.models import Account  # noqa: E402
from cases.models import Case  # noqa: E402
from common.models import Org, Profile, User  # noqa: E402
from contacts.models import Contact  # noqa: E402
from opportunity.models import DealPipeline, DealStage, Opportunity  # noqa: E402

ORG_NAME = "OpenOntology Demo Org"


def _get_or_create_org() -> Org:
    org = Org.objects.filter(name=ORG_NAME).first()
    if org:
        return org
    return Org.objects.create(name=ORG_NAME, default_currency="USD")


def _get_or_create_user(email: str, name: str) -> User:
    user = User.objects.filter(email=email).first()
    if user:
        return user
    return User.objects.create(
        id=uuid.uuid4().hex,
        email=email,
        name=name,
        is_active=True,
        is_staff=True,
    )


def _get_or_create_profile(org: Org, user: User) -> Profile:
    profile = Profile.objects.filter(org=org, user=user).first()
    if profile:
        return profile
    return Profile.objects.create(
        org=org,
        user=user,
        role="ADMIN",
        is_active=True,
        is_organization_admin=True,
        has_sales_access=True,
        has_marketing_access=True,
    )


def _get_or_create_pipeline(org: Org) -> DealPipeline:
    pipeline = DealPipeline.objects.filter(org=org, is_default=True).first()
    if pipeline:
        return pipeline
    pipeline = DealPipeline.objects.create(
        org=org, name="Sales Pipeline", is_default=True
    )
    defaults = [
        ("PROSPECTING", "Prospecting", 1, "open"),
        ("QUALIFICATION", "Qualification", 2, "open"),
        ("PROPOSAL", "Proposal", 3, "open"),
        ("NEGOTIATION", "Negotiation", 4, "open"),
        ("CLOSED_WON", "Closed Won", 5, "won"),
        ("CLOSED_LOST", "Closed Lost", 6, "lost"),
    ]
    for code, label, order, kind in defaults:
        DealStage.objects.create(
            org=org, pipeline=pipeline, code=code, label=label, order=order, kind=kind
        )
    return pipeline


def seed() -> dict[str, int]:
    org = _get_or_create_org()
    user = _get_or_create_user("owner@openontology.local", "Demo Owner")
    _get_or_create_profile(org, user)
    pipeline = _get_or_create_pipeline(org)

    accounts_data = [
        ("ACME-CHINA", "Acme China Manufacturing", "acme.cn@example.com", "Shanghai", "CN"),
        ("NORTHWIND", "Northwind Trading", "ops@northwind.example.com", "Beijing", "CN"),
        ("SOUTHSTAR", "Southstar Medical", "buyer@southstar.example.com", "Guangzhou", "CN"),
    ]
    accounts: dict[str, Account] = {}
    for code, name, email, city, country in accounts_data:
        account = Account.objects.filter(org=org, name=name).first()
        if not account:
            account = Account.objects.create(
                org=org,
                name=name,
                email=email,
                city=city,
                country=country,
                industry="MANUFACTURING",
                annual_revenue=Decimal("5000000.00"),
                currency="USD",
            )
        accounts[code] = account

    # a real contact linked to the first account
    if not Contact.objects.filter(org=org, email="cto@acme.cn.example.com").exists():
        Contact.objects.create(
            org=org,
            account=accounts["ACME-CHINA"],
            first_name="Wei",
            last_name="Zhang",
            email="cto@acme.cn.example.com",
            phone="+8613800000001",
        )

    opps_data = [
        ("OPP-2026-001", "Acme 产线升级一期", "ACME-CHINA", "NEGOTIATION", "48000.00"),
        ("OPP-2026-002", "Northwind 年度框架采购", "NORTHWIND", "PROPOSAL", "12000.00"),
        ("OPP-2026-003", "Southstar 影像设备替换", "SOUTHSTAR", "QUALIFICATION", "260000.00"),
    ]
    for code, name, account_code, stage, amount in opps_data:
        if Opportunity.objects.filter(org=org, name=name).exists():
            continue
        Opportunity.objects.create(
            org=org,
            pipeline=pipeline,
            name=name,
            account=accounts[account_code],
            stage=stage,
            amount=Decimal(amount),
            currency="USD",
            probability=75,
            lead_source="OUTBOUND",
            description=f"{name} (seeded from real BottleCRM ORM)",
            amount_source="MANUAL",
        )

    cases_data = [
        ("CASE-2026-001", "Acme 到货外包装破损", "ACME-CHINA", "New", "High", "Damaged packaging on arrival"),
        ("CASE-2026-002", "Northwind 发票金额不一致", "NORTHWIND", "In Progress", "Medium", "Invoice mismatch"),
        ("CASE-2026-003", "Southstar 设备延迟交付投诉", "SOUTHSTAR", "New", "Urgent", "Late delivery complaint"),
    ]
    for code, name, account_code, status, priority, desc in cases_data:
        if Case.objects.filter(org=org, name=name).exists():
            continue
        Case.objects.create(
            org=org,
            name=name,
            account=accounts[account_code],
            status=status,
            priority=priority,
            case_type="Problem",
            description=f"{desc} [ref {code}]",
        )

    return {
        "orgs": Org.objects.count(),
        "users": User.objects.count(),
        "accounts": Account.objects.count(),
        "contacts": Contact.objects.count(),
        "opportunities": Opportunity.objects.count(),
        "cases": Case.objects.count(),
        "pipelines": DealPipeline.objects.count(),
        "stages": DealStage.objects.count(),
    }


if __name__ == "__main__":
    counts = seed()
    print("seeded real BottleCRM tables:", counts)
