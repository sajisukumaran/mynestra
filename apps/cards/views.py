"""Cards views (tenant-scoped, member-accessible). Full set added in C4."""

from django.shortcuts import render


def dashboard(request):
    return render(request, "cards/dashboard.html", {})
