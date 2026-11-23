"""Preparación y cierre de la demo en vivo.

    python infra/scripts/demo.py prepare    # ~10 min antes de presentar
    python infra/scripts/demo.py check      # verificación rápida (sin cambiar nada)
    python infra/scripts/demo.py teardown   # al terminar: apaga tareas y limita el endpoint

`prepare`:
1. Verifica las credenciales del lab y que el endpoint de SageMaker esté InService.
2. Habilita el auto-scaling del endpoint (1–2 instancias) para poder mostrarlo.
3. Enciende los servicios ECS y espera a que estén estables.
4. Espera a que el NLB marque sano al ALB interno (si no, API Gateway responde 500).
5. Calienta el sistema (primeras consultas a Athena y al modelo) para que la demo no espere.
6. Escribe `build/postman_environment.json` (URLs + API keys, fuera de git) para importar en Postman.
7. Corre una verificación de cada punto de la demo e imprime las URLs.
Las API keys se escriben solo en ese archivo local; no se imprimen en pantalla.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ecs_deploy as d  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "build" / "postman_environment.json"
SM_RESOURCE = "endpoint/reco-als/variant/AllTraffic"


def call(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None, timeout: int = 40):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"content-type": "application/json", **(headers or {})},
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, code = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read(), exc.code
    except Exception as exc:  # timeouts, DNS
        return 0, {"error": type(exc).__name__}, 0.0
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return code, data, (time.perf_counter() - start) * 1000


def secrets(a: d.Aws) -> dict:
    return json.loads(a.secretsmanager.get_secret_value(SecretId=a.state["secret_arn"])["SecretString"])


def sagemaker_capacity(a: d.Aws, max_instances: int) -> None:
    sm = a.s.client("sagemaker")
    for _ in range(40):
        if sm.describe_endpoint(EndpointName="reco-als")["EndpointStatus"] == "InService":
            break
        time.sleep(15)
    a.application_autoscaling.register_scalable_target(
        ServiceNamespace="sagemaker", ResourceId=SM_RESOURCE,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount", MinCapacity=1, MaxCapacity=max_instances,
    )
    d.log(f"SageMaker: auto-scaling 1-{max_instances} instancias")


def wait_nlb(a: d.Aws) -> None:
    d.log("esperando que el NLB marque sano al ALB interno (hasta ~4 min)")
    for _ in range(30):
        health = a.elbv2.describe_target_health(TargetGroupArn=a.state["tg"]["nlb_alb"])["TargetHealthDescriptions"]
        if health and all(h["TargetHealth"]["State"] == "healthy" for h in health):
            d.log("NLB -> ALB interno: healthy")
            return
        time.sleep(10)
    d.log("ATENCIÓN: el NLB todavía no marca sano al ALB; API Gateway puede responder 500 unos minutos más")


def check(a: d.Aws) -> bool:
    st, keys = a.state, secrets(a)
    alb, api = f"http://{st['alb_public_dns']}", st["api_url"]
    k = {"x-api-key": keys["DASHBOARD_API_KEY"]}
    results = [
        ("Dashboard", *call(alb + "/")[:1], 200),
        ("Swagger /docs", *call(api + "/docs")[:1], 200),
        ("Widget (ALB público)", *call(alb + "/v1/recommendations?user_id=257597&k=5", headers={"x-api-key": keys["WIDGET_API_KEY"]})[:1], 200),
        ("API sin key -> 403", *call(api + "/v1/insights/kpis")[:1], 403),
        ("API predict", *call(api + "/v1/recommendations/predict", "POST", {"user_id": 257597, "k": 5}, k)[:1], 200),
        ("API KPIs (Athena)", *call(api + "/v1/insights/kpis", headers=k)[:1], 200),
        ("API promociones", *call(api + "/v1/insights/promotions?limit=5", headers=k)[:1], 200),
    ]
    ok = True
    for name, code, expected in results:
        mark = "OK " if code == expected else "FALLA"
        ok &= code == expected
        print(f"  [{mark}] {name:<24} HTTP {code} (esperado {expected})")
    code, body, _ = call(api + "/v1/recommendations/predict", "POST", {"user_id": 257597, "k": 5}, k)
    if code == 200:
        print(f"  predict -> estrategia={body.get('strategy')} respaldo={body.get('degraded')} (debe ser als / False)")
        ok &= body.get("strategy") == "als" and not body.get("degraded")
    return ok


def prepare() -> None:
    a = d.Aws()
    d.log("credenciales OK")
    sagemaker_capacity(a, 2)
    d.scale(a, 1)
    wait_nlb(a)
    keys = secrets(a)
    k = {"x-api-key": keys["DASHBOARD_API_KEY"]}
    d.log("calentando: Athena (KPIs y promociones), modelo y dashboard")
    api, alb = a.state["api_url"], f"http://{a.state['alb_public_dns']}"
    for _ in range(3):
        call(api + "/v1/insights/kpis", headers=k)
        call(api + "/v1/insights/promotions?limit=15", headers=k)
        call(api + "/v1/recommendations/predict", "POST", {"user_id": 257597, "k": 6}, k)
        call(alb + "/")
        time.sleep(2)
    env = {
        "name": "Recomendador EP3 (demo)",
        "values": [
            {"key": "api_url", "value": api, "enabled": True},
            {"key": "alb_url", "value": alb, "enabled": True},
            {"key": "api_key", "value": keys["DASHBOARD_API_KEY"], "enabled": True, "type": "secret"},
            {"key": "partner_key", "value": keys["PARTNER_API_KEY"], "enabled": True, "type": "secret"},
            {"key": "widget_key", "value": keys["WIDGET_API_KEY"], "enabled": True, "type": "secret"},
        ],
    }
    ENV_FILE.parent.mkdir(exist_ok=True)
    ENV_FILE.write_text(json.dumps(env, indent=2), encoding="utf-8")
    d.log(f"environment de Postman: {ENV_FILE.relative_to(ROOT)} (contiene las API keys: no compartir)")
    print("\nVerificación:")
    ok = check(a)
    print("\nURLs para la demo:")
    d.status(a)
    print("\nListo para presentar." if ok else "\nHay puntos con FALLA: revisar antes de presentar (ver plan de contingencia).")


def teardown() -> None:
    a = d.Aws()
    d.scale(a, 0)
    sagemaker_capacity(a, 1)
    d.log("demo cerrada: tareas apagadas y endpoint limitado a 1 instancia")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "prepare":
        prepare()
    elif command == "teardown":
        teardown()
    else:
        a = d.Aws()
        sys.exit(0 if check(a) else 1)


if __name__ == "__main__":
    main()
