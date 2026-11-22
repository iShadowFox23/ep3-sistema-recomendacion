"""Pruebas del cliente de business-api usado por el dashboard (CP-15 a nivel de cliente)."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import ApiError, BusinessApi, parse_items  # noqa: E402


def make(handler, api_key=None):
    return BusinessApi("http://api", api_key=api_key, transport=httpx.MockTransport(handler))


def test_sends_api_key_and_query_params():
    seen = {}

    def handler(request):
        seen["key"], seen["params"] = request.headers.get("x-api-key"), dict(request.url.params)
        return httpx.Response(200, json={"from": "a", "to": "b", "summary": {}, "daily": []})

    make(handler, api_key="k-1").kpis("2015-09-01", "2015-09-10")
    assert seen == {"key": "k-1", "params": {"from": "2015-09-01", "to": "2015-09-10"}}


def test_predict_omits_empty_fields():
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"items": []})

    make(handler).predict(None, "home", 5, [], None)
    assert sent == {"context": "home", "k": 5, "recent_items": []}


def test_api_error_uses_common_error_format():
    def handler(request):
        return httpx.Response(503, json={"error": {"code": "reco_unavailable", "message": "caído", "request_id": "r"}})

    with pytest.raises(ApiError) as exc:
        make(handler).predict(1, "home", 5, [], None)
    assert exc.value.status == 503 and exc.value.code == "reco_unavailable"


def test_unreachable_api_is_reported_clearly():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ApiError) as exc:
        make(handler).kpis()
    assert exc.value.code == "unreachable"


def test_download_relative_url_goes_through_api():
    def handler(request):
        assert request.url.path == "/v1/reports/abc/download"
        return httpx.Response(200, content=b"a,b\n1,2\n")

    assert make(handler).download("/v1/reports/abc/download") == b"a,b\n1,2\n"


def test_parse_items():
    assert parse_items("355908, 12 x 13") == [355908, 12, 13]
    assert parse_items("") == []
