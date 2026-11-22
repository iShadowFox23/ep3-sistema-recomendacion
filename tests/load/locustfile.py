"""Pruebas de carga (CP-14) del flujo del widget: ALB público → reco-service → rank-service → SageMaker.

Es el camino crítico del caso (requisito p95 < 200 ms). La mezcla de tráfico imita la tienda:
60% home de visitantes con historial, 25% ficha de producto, 5% carrito y 10% visitantes
anónimos (cold-start). Los IDs de usuario se eligen al azar entre 5.000, por lo que la mayoría
de las peticiones NO se sirven desde la caché (peor caso para el modelo).

Escenarios (variable SCENARIO):
    normal  30 usuarios durante 6 min                       (~30 req/s, carga sostenida)
    pico    0 → 150 usuarios en 30 s, 3 min arriba, bajada  (campaña tipo CyberDay)
    estres  +50 usuarios cada 60 s hasta 400 o hasta superar 5% de errores (punto de quiebre)

Uso:
    set WIDGET_API_KEY=...   (la del secreto reco-ep3/app; no se guarda en el repo)
    SCENARIO=normal locust -f tests/load/locustfile.py --host http://<alb-publico> --headless \
        --csv reports/load/normal --html reports/load/normal.html
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

from locust import LoadTestShape, between, events, task
from locust.contrib.fasthttp import FastHttpUser

DATA = json.loads((Path(__file__).parent / "data.json").read_text())
SCENARIO = os.getenv("SCENARIO", "normal")
API_KEY = os.getenv("WIDGET_API_KEY", "")
COUNTS = {"ok": 0, "degraded": 0, "cache_hit": 0}


@events.test_stop.add_listener
def write_counts(environment, **_):
    out = Path(os.getenv("COUNTS_OUT", f"reports/load/{SCENARIO}_counts.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {**COUNTS, "degraded_pct": round(100 * COUNTS["degraded"] / max(COUNTS["ok"], 1), 2),
            "cache_hit_pct": round(100 * COUNTS["cache_hit"] / max(COUNTS["ok"], 1), 2)}
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"respuestas ok={COUNTS['ok']} en respaldo={data['degraded_pct']}% desde caché={data['cache_hit_pct']}%")


class ShopVisitor(FastHttpUser):
    wait_time = between(0.5, 1.5)

    def _get(self, params: str, name: str) -> None:
        with self.client.get(
            f"/v1/recommendations?{params}", headers={"x-api-key": API_KEY}, name=name, catch_response=True
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            body = resp.json()
            if not body.get("items"):
                resp.failure("respuesta sin productos")
            else:
                # Responde, pero puede ser el respaldo: se cuenta aparte (no altera los percentiles).
                COUNTS["ok"] += 1
                COUNTS["degraded"] += bool(body.get("degraded"))
                COUNTS["cache_hit"] += body.get("cache") == "hit"

    @task(60)
    def home(self):
        self._get(f"user_id={random.choice(DATA['users'])}&context=home&k=10", "home (usuario conocido)")

    @task(25)
    def product_page(self):
        self._get(f"user_id={random.choice(DATA['users'])}&context=pdp&k=6&item_id={random.choice(DATA['items'])}", "ficha de producto")

    @task(5)
    def cart(self):
        items = "&".join(f"item_id={i}" for i in random.sample(DATA["items"], 2))
        self._get(f"user_id={random.choice(DATA['users'])}&context=cart&k=4&{items}", "carrito")

    @task(10)
    def anonymous(self):
        self._get(f"context=home&k=10&category_id={random.choice(DATA['categories'])}", "anónimo (cold-start)")


class ScenarioShape(LoadTestShape):
    """(fin en segundos, usuarios, usuarios por segundo al subir)"""

    STAGES = {
        "normal": [(360, 30, 5)],
        "pico": [(30, 150, 5), (210, 150, 5), (240, 10, 10)],
        "estres": [(60 * (i + 1), 50 * (i + 1), 10) for i in range(8)],
    }

    def tick(self):
        run_time = self.get_run_time()
        for end, users, rate in self.STAGES[SCENARIO]:
            if run_time < end:
                return users, rate
        return None


@events.init.add_listener
def stop_on_breaking_point(environment, **_):
    """En el escenario de estrés se detiene al superar 5% de errores (tras 60 s de calentamiento)."""
    if SCENARIO != "estres" or environment.runner is None:
        return

    def check(**__):
        stats = environment.runner.stats.total
        if environment.runner.state == "running" and stats.num_requests > 500 and stats.fail_ratio > 0.05:
            print(f"Punto de quiebre: {stats.fail_ratio:.1%} de errores con {environment.runner.user_count} usuarios")
            environment.runner.quit()

    import gevent

    def loop():
        while True:
            gevent.sleep(5)
            check()

    gevent.spawn(loop)
