import json
from gecko.onboard import cache_spec, safe_name


def test_cache_writes_and_roundtrips(tmp_path):
    path = cache_spec("stripe", {"openapi": "3.0.3"}, home=tmp_path)
    assert path.exists()
    assert json.loads(path.read_text())["openapi"] == "3.0.3"
    assert path.parent == tmp_path / ".gecko" / "surfaces"


def test_safe_name_sanitizes():
    assert safe_name("https://api.example.com/openapi.json") == "api-example-com"
    assert " " not in safe_name("My API") and "/" not in safe_name("a/b")
