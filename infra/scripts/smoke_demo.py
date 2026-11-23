"""Prueba de humo de la demo: corre los casos de la colección de Postman sin abrir Postman.

    python infra/scripts/smoke_demo.py          # casos de la demo + reporte asíncrono + traza en Logs Insights
    python infra/scripts/smoke_demo.py --full   # además CP-12 (reintentos -> DLQ)

Lee las peticiones desde `tests/postman/reco-ep3.postman_collection.json` y las variables desde
`build/postman_environment.json` (lo genera `demo.py prepare`), así prueba exactamente lo que se mostrará.
Usa request ids `smoke-*` para no ensuciar la consulta guardada `reco-ep3/demo-traza` (filtra `demo-home-001`).
No ejecuta CP-08 (rate limiting) para no gastar la cuota del plan partner antes de la demo.
No imprime API keys. Sale con código 1 si algún punto falla.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo  # noqa: E402
import ecs_deploy as d  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COLLECTION = ROOT / "tests" / "postman" / "reco-ep3.postman_collection.json"
LOG_GROUP = "/ecs/reco-ep3"
TRACE_ID = "smoke-home-001"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK ' if ok else 'FALLA'}] {name:<40} {detail}")


def load() -> tuple[dict[str, dict], dict[str, str]]:
    coll = json.loads(COLLECTION.read_text(encoding="utf-8"))
    items = {i["name"]: i["request"] for folder in coll["item"] for i in folder["item"]}
    env = json.loads(demo.ENV_FILE.read_text(encoding="utf-8"))
    return items, {v["key"]: v["value"] for v in env["values"]}


def run(req: dict, env: dict[str, str], request_id: str | None = None):
    sub = lambda s: re.sub(r"\{\{(\w+)\}\}", lambda m: env.get(m.group(1), m.group(0)), s)  # noqa: E731
    url = req["url"]["raw"] if isinstance(req["url"], dict) else req["url"]
    headers = {h["key"]: sub(h["value"]) for h in req.get("header", []) if not h.get("disabled")}
    if request_id and "x-request-id" in headers:
        headers["x-request-id"] = request_id
    raw = (req.get("body") or {}).get("raw")
    body = json.loads(sub(raw)) if raw else None
    return demo.call(sub(url), req["method"], body, headers)


def case(items, env, name: str, check, request_id: str | None = None):
    code, body, ms = run(items[name], env, request_id)
    try:
        ok, detail = check(code, body)
    except Exception as exc:  # cuerpo inesperado
        ok, detail = False, f"respuesta inesperada ({type(exc).__name__})"
    record(name.split(" ->")[0][:40], ok, f"HTTP {code}, {ms:.0f} ms" + (f" · {detail}" if detail else ""))
    return code, body


def poll_report(items, env, name: str, job_id: str, final: set[str], wait_s: int) -> dict:
    env = {**env, "job_id": job_id, "failed_job_id": job_id}
    body: dict = {}
    for _ in range(wait_s // 2):
        _, body, _ = run(items[name], env)
        if body.get("status") in final:
            break
        time.sleep(2)
    return body


def trace(a: d.Aws) -> None:
    logs = a.s.client("logs")
    query = f"fields @timestamp, @logStream | filter @message like /{TRACE_ID}/ | sort @timestamp asc"
    streams: set[str] = set()
    for _ in range(6):  # los logs tardan unos segundos en llegar
        time.sleep(10)
        qid = logs.start_query(logGroupName=LOG_GROUP, startTime=int(time.time()) - 900,
                               endTime=int(time.time()), queryString=query)["queryId"]
        while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
            time.sleep(1)
        streams = {f["value"].split("/")[0] for row in res["results"] for f in row if f["field"] == "@logStream"}
        if {"rank-service", "reco-service", "business-api"} <= streams:
            break
    ok = {"rank-service", "reco-service", "business-api"} <= streams
    record("Traza en Logs Insights", ok, ", ".join(sorted(streams)) or "sin registros")


def main() -> None:
    full = "--full" in sys.argv
    if not demo.ENV_FILE.exists():
        sys.exit("Falta build/postman_environment.json: correr antes `demo.py prepare`.")
    items, env = load()
    print("Colección de Postman (sin abrir Postman):")
    case(items, env, "CP-07 Sin API key -> 403", lambda c, b: (c == 403, ""))
    case(items, env, "CP-09 Ruta interna /v1/rank no expuesta -> 403", lambda c, b: (c in (403, 404), ""))
    case(items, env, "CP-05 Usuario conocido (home) -> ALS con explicación",
         lambda c, b: (c == 200 and len(b["items"]) == 5 and b["strategy"] == "als" and not b.get("degraded")
                       and all(isinstance(i["reason"], dict) for i in b["items"]),
                       f"estrategia={b.get('strategy')} respaldo={b.get('degraded')}"), request_id=TRACE_ID)
    case(items, env, "Ficha de producto -> productos parecidos",
         lambda c, b: (b.get("strategy") == "item_similarity", f"estrategia={b.get('strategy')}"), request_id="smoke-pdp-001")
    case(items, env, "CP-04 Visitante nuevo (cold-start)", lambda c, b: (b.get("cold_start") is True, ""))
    case(items, env, "CP-06 Contexto inválido -> 422",
         lambda c, b: (c == 422 and b["error"]["code"] == "invalid_request", ""))
    case(items, env, "KPIs del embudo (Athena)", lambda c, b: (c == 200 and b["summary"]["visitors"] > 0, ""))
    case(items, env, "Promociones sugeridas (prescriptivo)",
         lambda c, b: (c == 200 and all(isinstance(i["reason"], str) for i in b["items"]), ""))
    case(items, env, "Widget home (API key de la tienda)", lambda c, b: (c == 200, ""))

    code, body = case(items, env, "CP-11a Solicitar reporte -> 202", lambda c, b: (c == 202 and "job_id" in b, ""))
    if code == 202:
        rep = poll_report(items, env, "CP-11b Estado del reporte (repetir hasta done)", body["job_id"], {"done", "failed"}, 60)
        url = rep.get("download_url")
        dl_code = demo.call(url)[0] if url else 0
        # el CSV no es JSON: demo.call devuelve el código igual
        record("CP-11b Reporte terminado y descargable", rep.get("status") == "done" and dl_code == 200,
               f"status={rep.get('status')}, descarga HTTP {dl_code}")

    if full:
        code, body = case(items, env, "CP-12 Reporte con falla forzada (reintentos -> DLQ)", lambda c, b: (c == 202, ""))
        if code == 202:
            rep = poll_report(items, env, "CP-12 Estado del reporte fallido (esperar ~10 s)", body["job_id"], {"failed"}, 40)
            record("CP-12 Reintentos -> DLQ", rep.get("status") == "failed",
                   f"status={rep.get('status')}, intentos={rep.get('attempts')}")

    print("\nTraza distribuida (CloudWatch Logs Insights, grupo /ecs/reco-ep3):")
    trace(d.Aws())

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK." + (f" Revisar: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
