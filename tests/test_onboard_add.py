import json

from gecko.onboard import AddDeps, add

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Stripe", "version": "1"},
    "components": {"securitySchemes": {"k": {"type": "apiKey"}}},
    "paths": {},
}


def test_add_end_to_end_with_fakes(tmp_path, capsys):
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: calls.append(("store", n)),
        run=lambda cmd: calls.append(("run", cmd)) or 0,
        home=tmp_path,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("store", "api-stripe-com") in calls
    assert (tmp_path / ".gecko" / "surfaces" / "api-stripe-com.json").exists()
    assert "47" in out and "ask your agent" in out.lower()
