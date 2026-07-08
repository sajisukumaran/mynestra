"""Login/logout + password reset (email) with the custom email-login user."""


def test_login_and_logout(make_user, client):
    make_user("login@example.com", password="sup3rsecret")

    response = client.post(
        "/login/", {"username": "login@example.com", "password": "sup3rsecret"}
    )
    assert response.status_code == 302  # -> LOGIN_REDIRECT_URL

    response = client.post("/logout/")  # Django 5 logout is POST-only
    assert response.status_code == 302


def test_login_rejects_bad_password(make_user, client):
    make_user("login@example.com", password="sup3rsecret")
    response = client.post("/login/", {"username": "login@example.com", "password": "wrong"})
    assert response.status_code == 200  # re-renders the form with errors


def test_password_reset_sends_email(make_user, client, mailoutbox):
    make_user("reset@example.com")
    response = client.post("/password-reset/", {"email": "reset@example.com"})
    assert response.status_code == 302
    assert len(mailoutbox) == 1
    assert "reset@example.com" in mailoutbox[0].to
