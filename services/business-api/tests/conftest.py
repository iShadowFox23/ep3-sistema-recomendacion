import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.analytics import LocalAnalytics
from src.catalog import Catalog, ItemInfo
from src.jobs import FileJobStore
from src.main import app
from src.settings import Components
from src.storage import LocalReportStore


def make_analytics() -> LocalAnalytics:
    days = pd.date_range("2015-09-01", "2015-09-20").strftime("%Y-%m-%d")
    kpis = pd.DataFrame(
        {
            "date": days,
            "visitors": 100,
            "new_visitors": 80,
            "buyers": 2,
            "views": 150,
            "carts": 6,
            "transactions": 3,
        }
    )
    rows = []
    for d in days:
        rows += [
            (d, 1, 10, 1, 1),  # convierte 10%: destacar
            (d, 2, 10, 0, 0),  # sin carritos: no se sugiere
            (d, 3, 30, 3, 0),  # muchas vistas y carritos sin compra: descuento
            (d, 4, 10, 1, 1),  # convierte bien pero sin stock: excluido
            (d, 5, 5, 0, 0),
        ]
    items = pd.DataFrame(rows, columns=["date", "itemid", "views", "carts", "transactions"])
    return LocalAnalytics(kpis, items)


CATALOG = Catalog(
    {
        1: ItemInfo(category_id=10, available=True),
        2: ItemInfo(category_id=10, available=True),
        3: ItemInfo(category_id=10, available=True),
        4: ItemInfo(category_id=10, available=False),
        5: ItemInfo(category_id=20, available=True),
    }
)


class FakePublisher:
    def __init__(self, fail=False):
        self.fail, self.messages = fail, []

    def publish_job(self, message):
        from src.broker import BrokerUnavailable

        if self.fail:
            raise BrokerUnavailable("connection refused")
        self.messages.append(message)


@pytest.fixture()
def components(tmp_path):
    return Components(
        analytics=make_analytics(),
        catalog=CATALOG,
        jobs=FileJobStore(str(tmp_path / "jobs")),
        reports=LocalReportStore(str(tmp_path / "reports")),
    )


@pytest.fixture()
def reco_calls():
    return []


@pytest.fixture()
def client(components, reco_calls):
    def reco_handler(request: httpx.Request) -> httpx.Response:
        reco_calls.append(request)
        return httpx.Response(200, json={"items": [{"item_id": 1}], "strategy": "als", "degraded": False})

    app.state.components = components
    app.state.publisher = FakePublisher()
    app.state.http = httpx.AsyncClient(base_url="http://reco", transport=httpx.MockTransport(reco_handler))
    with TestClient(app) as c:
        yield c
    for attr in ("components", "publisher", "http"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
