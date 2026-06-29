from gecko.access import (
    AUTH_APITOKEN_HEADER,
    AUTH_JWT_HEADER,
    Session,
    activation_message,
    establish_session,
)


def test_two_token_auth_headers():
    s = Session(jwt="JWT", api_token="APITOK")
    headers = s.auth_headers()
    assert headers[AUTH_JWT_HEADER] == "Bearer JWT"
    assert headers[AUTH_APITOKEN_HEADER] == "APITOK"


def test_activation_message_format():
    assert activation_message("TX", [1, 2], "JWT") == b"TX:1,2:JWT"
    assert activation_message("TX", [], "JWT") == b"TX::JWT"


def test_establish_session_flow_is_correct():
    calls = []

    def fake_transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if url.endswith("/auth/guest/start"):
            return 200, {"token": "THE_JWT"}
        if url.endswith("/api/token/activate"):
            # activate must carry the JWT as bearer and the signed payload
            assert headers[AUTH_JWT_HEADER] == "Bearer THE_JWT"
            assert body["txSig"] == "TXSIG"
            assert body["walletSignature"] == "SIG_B64"
            return 200, {"token": "THE_API_TOKEN"}
        raise AssertionError(f"unexpected url {url}")

    def fake_signer(message: bytes) -> str:
        assert message == b"TXSIG:39:THE_JWT"  # message must include the live JWT
        return "SIG_B64"

    session = establish_session(
        "https://txline.txodds.com",
        "TXSIG",
        [39],
        fake_signer,
        transport=fake_transport,
    )
    assert session.jwt == "THE_JWT"
    assert session.api_token == "THE_API_TOKEN"
    assert [c[0] for c in calls] == ["POST", "POST"]
