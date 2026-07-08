"""Invitation create (Owner-only) + tokened accept (new vs existing user), expiry, single-use."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.tenants.models import Invitation, Membership, Role


def test_owner_can_create_and_email_invitation(make_tenant, make_user, client, mailoutbox):
    tenant = make_tenant(name="Acme")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)

    client.force_login(owner)
    response = client.post(
        f"/t/{tenant.schema_name}/setup/members/invite/",
        {"email": "invitee@example.com", "role": "MEMBER"},
    )
    assert response.status_code == 302
    assert Invitation.objects.filter(email="invitee@example.com", tenant=tenant).exists()
    assert len(mailoutbox) == 1
    assert "invitee@example.com" in mailoutbox[0].to


def test_member_cannot_manage_members(make_tenant, make_user, client):
    tenant = make_tenant(name="Acme")
    member = make_user("member@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)

    client.force_login(member)
    # Setup → Members is Owner-only.
    assert client.get(f"/t/{tenant.schema_name}/setup/members/").status_code == 403
    assert (
        client.post(
            f"/t/{tenant.schema_name}/setup/members/invite/",
            {"email": "x@example.com", "role": "MEMBER"},
        ).status_code
        == 403
    )
    assert not Invitation.objects.filter(email="x@example.com", tenant=tenant).exists()


def test_accept_as_new_user_creates_account_and_membership(make_tenant, client):
    tenant = make_tenant(name="Acme")
    invitation = Invitation.objects.create(email="new@example.com", tenant=tenant, role=Role.MEMBER)

    response = client.post(
        invitation.get_accept_path(), {"full_name": "New User", "password": "sup3rsecret"}
    )
    assert response.status_code == 302

    user = get_user_model().objects.get(email="new@example.com")
    assert Membership.objects.filter(user=user, tenant=tenant).exists()
    invitation.refresh_from_db()
    assert invitation.status == Invitation.Status.ACCEPTED


def test_accept_as_existing_user_just_joins(make_tenant, make_user, client):
    tenant = make_tenant(name="Acme")
    existing = make_user("exists@example.com")
    invitation = Invitation.objects.create(
        email="exists@example.com", tenant=tenant, role=Role.MEMBER
    )

    response = client.post(invitation.get_accept_path())
    assert response.status_code == 302
    assert Membership.objects.filter(user=existing, tenant=tenant).exists()
    invitation.refresh_from_db()
    assert invitation.status == Invitation.Status.ACCEPTED


def test_used_token_is_rejected(make_tenant, client):
    tenant = make_tenant(name="Acme")
    invitation = Invitation.objects.create(
        email="new@example.com", tenant=tenant, status=Invitation.Status.ACCEPTED
    )
    assert client.get(invitation.get_accept_path()).status_code == 410


def test_expired_token_is_rejected(make_tenant, client):
    tenant = make_tenant(name="Acme")
    invitation = Invitation.objects.create(
        email="new@example.com", tenant=tenant, expires_at=timezone.now() - timedelta(days=1)
    )
    assert client.get(invitation.get_accept_path()).status_code == 410
