def test_health_endpoint_returns_200(client, public_tenant):
    response = client.get("/health/")
    assert response.status_code == 200
    body = response.content.decode().lower()
    assert "mynestra" in body
    assert "ok" in body  # DB connectivity line renders "ok" when SELECT 1 succeeds
