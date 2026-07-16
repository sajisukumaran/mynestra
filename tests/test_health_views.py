"""Health screens (authenticated tenant client): dashboard / visits / providers / invoices render;
creating a visit and a provider invoice through the locked-bill path; recording a payment (bank and
HSA) through the pay view; the pending-insurance → confirm flow; and tenant isolation."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user, name="Health Household", email="owner@health.test"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _c(tenant, path=""):
    return f"/t/{tenant.schema_name}/{path}"


def _org(name="City Hospital"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _person(first="Sam", last="Rivera"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


# --- screens render --------------------------------------------------------------------------

def test_dashboard_and_lists_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert "Health" in client.get(_c(tenant, "health/")).content.decode()
    assert "Visits" in client.get(_c(tenant, "health/visits/")).content.decode()
    assert "provider" in client.get(_c(tenant, "health/providers/")).content.decode().lower()
    assert "invoice" in client.get(_c(tenant, "health/invoices/")).content.decode().lower()
    assert client.get(_c(tenant, "health/visits/new/")).status_code == 200
    assert client.get(_c(tenant, "health/invoices/new/")).status_code == 200


# --- create a visit + invoice, then pay ------------------------------------------------------

def test_create_visit_invoice_and_pay(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        patient = _person()
        biller = _org()
        pid, bid = patient.pk, biller.pk
    client.force_login(owner)

    # Create the visit.
    client.post(_c(tenant, "health/visits/new/"), {
        "patient": pid, "date": "2026-02-01", "encounter_type": "medical",
        "setting": "office", "visit_status": "completed", "reason": "Physical",
    })
    with schema_context(tenant.schema_name):
        from apps.health.models import Encounter

        enc = Encounter.objects.get()
        eid = enc.pk

    # Add an unpaid provider invoice under the visit (posts the locked bill).
    client.post(_c(tenant, f"health/visits/{eid}/invoices/new/"), {
        "biller_organization": bid, "invoice_date": "2026-02-01", "status": "unpaid",
        "amount_due": "250", "invoice_number": "H-1",
    })
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance
        from apps.health.models import ProviderInvoice

        inv = ProviderInvoice.objects.get()
        assert inv.bill_id is not None
        assert account_balance("medical_expense") == D("250")
        iid = inv.pk

    # Detail page renders and shows the biller.
    detail = client.get(_c(tenant, f"health/invoices/{iid}/"))
    assert detail.status_code == 200 and "City Hospital" in detail.content.decode()

    # Pay it in full from cash → PAID, AP settled.
    client.post(_c(tenant, f"health/invoices/{iid}/pay/"), {
        "amount": "250", "date": "2026-02-05", "funding": "cash",
    })
    with schema_context(tenant.schema_name):
        inv.refresh_from_db()
        assert inv.status == "paid"
        assert account_balance("accounts_payable") == D("0")


def test_pay_from_hsa_via_view(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.investments.models import InvestmentAccount, InvestmentTransaction, InvTxnType
        from apps.investments.services import apply_transaction, ensure_gl_account

        biller = _org()
        hsa = InvestmentAccount.objects.create(
            institution=_org("HSA Bank"), nickname="Family HSA", registration="hsa",
            currency=_usd(),
        )
        ensure_gl_account(hsa)
        opening = InvestmentTransaction.objects.create(
            account=hsa, txn_type=InvTxnType.OPENING, date=datetime.date(2026, 1, 1),
            amount=D("3000"),
        )
        apply_transaction(opening, is_new=True)
        bid, hid = biller.pk, hsa.pk

    client.force_login(owner)
    client.post(_c(tenant, "health/invoices/new/"), {
        "biller_organization": bid, "invoice_date": "2026-02-01", "status": "unpaid",
        "amount_due": "300",
    })
    with schema_context(tenant.schema_name):
        from apps.health.models import ProviderInvoice

        iid = ProviderInvoice.objects.get().pk

    client.post(_c(tenant, f"health/invoices/{iid}/pay/"), {
        "amount": "300", "date": "2026-02-05", "funding": "hsa", "hsa_account": hid,
    })
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance
        from apps.health.models import ProviderInvoice

        inv = ProviderInvoice.objects.get(pk=iid)
        assert inv.status == "paid"
        assert account_balance("accounts_payable") == D("0")
        assert account_balance(hsa.gl_account) == D("2700")  # HSA dropped by 300


def test_pending_then_confirm_flow(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bid = _org().pk
    client.force_login(owner)

    client.post(_c(tenant, "health/invoices/new/"), {
        "biller_organization": bid, "invoice_date": "2026-02-01",
        "status": "pending_insurance", "amount_due": "0",
    })
    with schema_context(tenant.schema_name):
        from apps.health.models import ProviderInvoice

        inv = ProviderInvoice.objects.get()
        assert inv.bill_id is None  # pending posts nothing
        iid = inv.pk

    client.post(_c(tenant, f"health/invoices/{iid}/confirm/"), {"amount_due": "180"})
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance

        inv.refresh_from_db()
        assert inv.status == "unpaid" and inv.bill_id is not None
        assert account_balance("medical_expense") == D("180")


# --- facility filtering + provider inline-create ---------------------------------------------

def test_facility_limited_to_medical_and_provider_inline_create(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user, name="Med HH", email="med@h.test")
    with schema_context(tenant.schema_name):
        from apps.organizations.models import Organization
        from apps.setup.models import Category

        hosp = Organization.objects.create(name="Mercy Clinic")
        hosp.categories.add(Category.objects.get(kind=Category.Kind.ORG, name="Hospital/Clinic"))
        Organization.objects.create(name="Big Bank Ltd")  # not medical — must be excluded
        pid = _person("Pat", "Ient").pk
    client.force_login(owner)

    # the facility <select> lists the medical org, not the bank (the roster affiliation picker,
    # which lists all orgs, is a separate control).
    body = client.get(_c(tenant, "health/visits/new/")).content.decode()
    start = body.index('name="facility"')
    facility_select = body[start:body.index("</select>", start)]
    assert "Mercy Clinic" in facility_select
    assert "Big Bank Ltd" not in facility_select

    # inline-create a provider + a facility while booking the visit
    client.post(_c(tenant, "health/visits/new/"), {
        "patient": pid, "date": "2026-03-01", "encounter_type": "medical",
        "setting": "office", "visit_status": "completed",
        "primary_provider_new_name": "Dana Okafor",
        "facility_new_name": "New Health Center",
    })
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person
        from apps.health.models import Encounter
        from apps.organizations.models import Organization

        doc = Person.objects.get(first_name="Dana", last_name="Okafor")
        assert doc.categories.filter(name="Doctor").exists()
        fac = Organization.objects.get(name="New Health Center")
        assert fac.categories.filter(name="Hospital/Clinic").exists()  # tagged medical on create
        enc = Encounter.objects.get()
        assert enc.primary_provider_id == doc.pk and enc.facility_id == fac.pk


def test_dashboard_provider_create(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user, name="Prov HH", email="prov@h.test")
    client.force_login(owner)
    client.post(_c(tenant, "health/providers/new/"), {
        "first_name": "Lee", "last_name": "Nguyen",
        "affiliation_new_name": "Downtown Family Practice",
    })
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person
        from apps.organizations.models import Organization
        from apps.relationships.models import PersonOrgRelationship

        doc = Person.objects.get(first_name="Lee", last_name="Nguyen")
        assert doc.categories.filter(name="Doctor").exists()
        prac = Organization.objects.get(name="Downtown Family Practice")
        assert prac.categories.filter(name="Hospital/Clinic").exists()
        assert PersonOrgRelationship.objects.filter(
            person=doc, organization=prac, type__code="provider_affiliation"
        ).exists()


def test_patient_picker_lists_household_members_only(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user, name="HH Only", email="hho@h.test")
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person

        Person.objects.create(first_name="Home", last_name="Member", is_household_member=True)
        Person.objects.create(first_name="Doc", last_name="External")  # a provider, not household
    client.force_login(owner)
    for path in ("health/visits/new/", "health/prescriptions/new/"):
        body = client.get(_c(tenant, path)).content.decode()
        start = body.index('name="patient"')
        patient_select = body[start:body.index("</select>", start)]
        assert "Home Member" in patient_select, path
        assert "Doc External" not in patient_select, path


# --- tenant isolation ------------------------------------------------------------------------

def test_health_tenant_isolation(make_tenant, make_user, client):
    a, owner_a = _owner(make_tenant, make_user, name="Alpha", email="a@h.test")
    b, owner_b = _owner(make_tenant, make_user, name="Beta", email="b@h.test")
    with schema_context(a.schema_name):
        from apps.health.models import Encounter

        enc = Encounter.objects.create(patient=_person("Alpha", "Only"), date="2026-01-01")
        eid = enc.pk

    client.force_login(owner_b)
    assert "Alpha Only" not in client.get(_c(b, "health/visits/")).content.decode()
    assert client.get(_c(b, f"health/visits/{eid}/")).status_code == 404
    assert client.get(_c(a, "health/visits/")).status_code == 403  # not a member of A

    client.force_login(owner_a)
    assert client.get(_c(a, f"health/visits/{eid}/")).status_code == 200
    with schema_context(b.schema_name):
        assert Encounter.objects.count() == 0  # zero leak into B
