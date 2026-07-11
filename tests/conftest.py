from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.utils import get_public_schema_name

from apps.tenants.models import Domain, Tenant

# The migrated + §6-seeded tenant schema built ONCE per session; make_tenant clones it per test
# instead of re-running the whole migration history (~6.7s/tenant). See tenant_template below.
TENANT_TEMPLATE_SCHEMA = "test_template"


def _drop_cross_schema_fks(schema):
    """Drop FK constraints in `schema` that reference the public schema (audit/GenericFK links).

    They're an artifact of django-tenants keeping contenttypes/auth/users shared; they exist in
    production tenant schemas but are not needed for test correctness. A persistent tenant schema
    with such FKs would block transactional_db's non-CASCADE `TRUNCATE public.*`.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ns.nspname, cl.relname, con.conname
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            JOIN pg_class fcl ON fcl.oid = con.confrelid
            JOIN pg_namespace fns ON fns.oid = fcl.relnamespace
            WHERE con.contype = 'f' AND ns.nspname = %s AND fns.nspname = 'public'
            """,
            [schema],
        )
        for nsp, rel, con in cursor.fetchall():
            cursor.execute(f'ALTER TABLE "{nsp}"."{rel}" DROP CONSTRAINT "{con}"')


@pytest.fixture(autouse=True)
def public_tenant(transactional_db):
    """Every request goes through TenantSubfolderMiddleware, which needs a public Tenant row.

    Resets the connection to public at BOTH the start and end of each test. A client request to
    /t/<slug>/ leaves the connection pinned to that tenant schema (django-tenants doesn't reset it
    between test-client requests). At start this would break tenant creation; at teardown it would
    make transactional_db's flush TRUNCATE the pinned tenant schema instead of `public`, so public
    tables (e.g. users_user) would survive and collide with the next test. Resetting here (this
    fixture tears down just before transactional_db flushes) keeps the flush pointed at public.
    """
    connection.set_schema_to_public()
    tenant, _ = Tenant.objects.get_or_create(
        schema_name=get_public_schema_name(), defaults={"name": "Public"}
    )
    yield tenant
    connection.set_schema_to_public()


@pytest.fixture(scope="session")
def tenant_template(django_db_setup, django_db_blocker):
    """Provision ONE tenant schema (full migrate + §6 seed) per session; make_tenant clones it.

    Cloning a pre-migrated schema (django-tenants CloneSchema, clone_mode="DATA" = structure AND
    rows) is ~tens of ms vs. ~6.7s for a fresh migrate — the dominant cost of the suite. Building
    the template via the real `Tenant.save()` exercises the actual provisioning path (migrate +
    post_schema_sync + all seeds) exactly once, so provisioning/seed tests still assert against it.

    Safe with transactional_db: the per-test flush runs on the `public` search_path (public_tenant
    resets there before teardown), so it only truncates `public.*` — the template's own
    `test_template.*` tables are never touched. Depends on django_db_setup so the test DB exists.

    One wrinkle handled below: unlike the old per-test schemas (dropped BEFORE the flush), the
    template persists, and tenant tables carry real cross-schema FKs into public (e.g.
    finance_journalentry.content_type_id -> django_content_type, Historical*.history_user_id ->
    users_user). Those would make the flush's non-CASCADE `TRUNCATE public.*` fail. We drop those
    audit/GenericFK constraints from the template (no test relies on their DB-level enforcement;
    all intra-tenant FKs like JournalLine.person PROTECT are kept). Clones inherit this and are
    dropped before the flush anyway.
    """
    with django_db_blocker.unblock():
        connection.set_schema_to_public()
        # Safe, test-DB-only durability flag (user-approved): ALTER DATABASE ... SET applies to
        # every connection to this DB. Guarded on the `test_` prefix so a real DB is never touched.
        db_name = connection.settings_dict["NAME"]
        with connection.cursor() as cursor:
            if db_name.startswith("test_"):
                cursor.execute(f'ALTER DATABASE "{db_name}" SET synchronous_commit TO off')
            cursor.execute("SET synchronous_commit TO off")
            # Defensive vs. a stale schema left by a previous run (e.g. --reuse-db).
            cursor.execute(f'DROP SCHEMA IF EXISTS "{TENANT_TEMPLATE_SCHEMA}" CASCADE')
        template = Tenant(schema_name=TENANT_TEMPLATE_SCHEMA, name="Template")
        template.save()  # CREATE SCHEMA + migrate + seed §6 (the real provisioning path, once)
        Tenant.objects.filter(pk=template.pk).delete()  # keep the SCHEMA; drop the public row
        _drop_cross_schema_fks(TENANT_TEMPLATE_SCHEMA)  # so the per-test public flush can TRUNCATE

    yield TENANT_TEMPLATE_SCHEMA

    with django_db_blocker.unblock():
        connection.set_schema_to_public()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{TENANT_TEMPLATE_SCHEMA}" CASCADE')


@pytest.fixture
def make_user():
    User = get_user_model()

    def _make(email, password="pw-testing-12345", **extra):
        return User.objects.create_user(email=email, password=password, **extra)

    return _make


@pytest.fixture
def make_tenant(tenant_template):
    """Create a tenant by CLONING the pre-migrated template (unique schema + Domain + §6 seed);
    drop the schema on teardown."""
    from django_tenants.clone import CloneSchema

    created = []

    def _make(name="Test Household", slug=None):
        connection.set_schema_to_public()  # tenants can only be created from the public schema
        slug = slug or f"t{uuid4().hex[:12]}"
        # Clone structure + seeded rows from the template. set_connection=False keeps us on public
        # (Tenant.save() raises if the connection isn't on the public schema).
        CloneSchema().clone_schema(
            tenant_template, slug, clone_mode="DATA", set_connection=False
        )
        connection.set_schema_to_public()
        tenant = Tenant(schema_name=slug, name=name)
        tenant.auto_create_schema = False  # schema already cloned → no migrate, no re-seed
        tenant.save()
        Domain.objects.get_or_create(domain=slug, tenant=tenant, defaults={"is_primary": True})
        created.append(tenant)
        return tenant

    yield _make

    for tenant in created:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema_name}" CASCADE')
        Tenant.objects.filter(pk=tenant.pk).delete()
