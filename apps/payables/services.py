"""Payables service layer — the sanctioned path to the GL, plus subledger helpers.

Vendor resolution lives here now; bill/payment posting joins it in later commits (everything that
touches the ledger goes through `apps.finance.services`, never direct JournalEntry/Line rows).
"""

from apps.payables.models import VendorProfile


def ensure_vendor_profile(*, person=None, organization=None) -> VendorProfile:
    """Get-or-create the VendorProfile for a Person or an Organization (exactly one)."""
    if person is not None:
        profile, _ = VendorProfile.objects.get_or_create(person=person)
    else:
        profile, _ = VendorProfile.objects.get_or_create(organization=organization)
    return profile
