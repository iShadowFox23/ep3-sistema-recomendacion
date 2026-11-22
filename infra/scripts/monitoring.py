"""Monitoreo en CloudWatch: alarmas, dashboard y extracción de métricas de una prueba de carga.

Alarmas (umbrales corregidos respecto de la EP2, alineados al SLA p95 < 200 ms):
    reco-latency-p95-warning    ALB TargetResponseTime p95 > 150 ms durante 5 min
    reco-latency-p95-critical   ALB TargetResponseTime p95 > 200 ms durante 5 min
    reco-5xx-rate               5xx del target > 5% de las peticiones durante 5 min
    model-latency-p95           SageMaker ModelLatency p95 > 100 ms durante 5 min
    reports-dlq                 (vía logs) mensajes enviados a reports.dlq
Las alarmas publican en el tópico SNS `reco-ep3-alerts` (sin suscriptores por defecto: agregar el
correo del equipo desde la consola si se quiere recibir avisos).

Uso:
    python infra/scripts/monitoring.py setup
    python infra/scripts/monitoring.py metrics --start 2026-11-22T06:40 --end 2026-11-22T06:50 --out reports/load/cw_normal.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
STATE = json.loads((ROOT / "build" / "aws_state.json").read_text())
P = "reco-ep3"
CLUSTER = f"{P}-cluster"


def dims_alb(tg_key: str = "reco_pub") -> list[dict]:
    lb = "/".join(STATE["alb_public"].split(":")[-1].split("/")[1:])
    tg = STATE["tg"][tg_key].split(":")[-1]
    return [{"Name": "LoadBalancer", "Value": lb}, {"Name": "TargetGroup", "Value": tg}]


SAGEMAKER_DIMS = [{"Name": "EndpointName", "Value": "reco-als"}, {"Name": "VariantName", "Value": "AllTraffic"}]


def setup() -> None:
    cw, sns, logs = boto3.client("cloudwatch"), boto3.client("sns"), boto3.client("logs")
    topic = sns.create_topic(Name=f"{P}-alerts", Tags=[{"Key": "project", "Value": P}])["TopicArn"]
    common = dict(AlarmActions=[topic], OKActions=[topic], EvaluationPeriods=5, DatapointsToAlarm=5, Period=60, TreatMissingData="notBreaching")
    for name, threshold in (("warning", 0.15), ("critical", 0.20)):
        cw.put_metric_alarm(
            AlarmName=f"{P}-reco-latency-p95-{name}", Namespace="AWS/ApplicationELB", MetricName="TargetResponseTime",
            Dimensions=dims_alb(), ExtendedStatistic="p95", Threshold=threshold, ComparisonOperator="GreaterThanThreshold",
            AlarmDescription=f"Latencia p95 del widget > {int(threshold * 1000)} ms (SLA: 200 ms)", **common,
        )
    cw.put_metric_alarm(
        AlarmName=f"{P}-reco-5xx-rate", AlarmDescription="Más de 5% de respuestas 5xx en reco-service",
        Metrics=[
            {"Id": "errors", "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": "HTTPCode_Target_5XX_Count", "Dimensions": dims_alb()}, "Period": 60, "Stat": "Sum"}, "ReturnData": False},
            {"Id": "total", "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": "RequestCount", "Dimensions": dims_alb()}, "Period": 60, "Stat": "Sum"}, "ReturnData": False},
            {"Id": "rate", "Expression": "IF(total > 0, 100 * errors / total, 0)", "Label": "% 5xx", "ReturnData": True},
        ],
        Threshold=5, ComparisonOperator="GreaterThanThreshold", AlarmActions=[topic], OKActions=[topic],
        EvaluationPeriods=5, DatapointsToAlarm=5, TreatMissingData="notBreaching",
    )
    cw.put_metric_alarm(
        AlarmName=f"{P}-model-latency-p95", Namespace="AWS/SageMaker", MetricName="ModelLatency", Dimensions=SAGEMAKER_DIMS,
        ExtendedStatistic="p95", Threshold=100_000, ComparisonOperator="GreaterThanThreshold",  # microsegundos
        AlarmDescription="Latencia p95 del modelo en SageMaker > 100 ms", **common,
    )
    logs.put_metric_filter(
        logGroupName=f"/ecs/{P}", filterName=f"{P}-dead-lettered",
        filterPattern='"dead_lettered"',
        metricTransformations=[{"metricName": "ReportsDeadLettered", "metricNamespace": "RecoEP3", "metricValue": "1", "defaultValue": 0}],
    )
    cw.put_metric_alarm(
        AlarmName=f"{P}-reports-dlq", Namespace="RecoEP3", MetricName="ReportsDeadLettered", Statistic="Sum", Period=300,
        EvaluationPeriods=1, Threshold=0, ComparisonOperator="GreaterThanThreshold", TreatMissingData="notBreaching",
        AlarmActions=[topic], AlarmDescription="Un reporte agotó sus reintentos y quedó en reports.dlq",
    )
    lb_dims = dims_alb()

    def widget(title, metrics, stat="p95", y=0, x=0, w=12):
        return {"type": "metric", "x": x, "y": y, "width": w, "height": 6,
                "properties": {"title": title, "region": boto3.Session().region_name, "stat": stat, "period": 60, "metrics": metrics, "view": "timeSeries"}}

    body = {"widgets": [
        widget("Widget: latencia p95 / p99 (s)", [["AWS/ApplicationELB", "TargetResponseTime", *sum(([d["Name"], d["Value"]] for d in lb_dims), []), {"stat": "p95"}],
                                                   ["...", {"stat": "p99"}]], y=0, x=0),
        widget("Widget: peticiones y 5xx por minuto", [["AWS/ApplicationELB", "RequestCount", *sum(([d["Name"], d["Value"]] for d in lb_dims), []), {"stat": "Sum"}],
                                                        [".", "HTTPCode_Target_5XX_Count", ".", ".", ".", ".", {"stat": "Sum"}]], stat="Sum", y=0, x=12),
        widget("ECS: CPU % por servicio", [["AWS/ECS", "CPUUtilization", "ClusterName", CLUSTER, "ServiceName", s] for s in ("reco-service", "rank-service", "business-api")], stat="Average", y=6, x=0),
        widget("ECS: tareas en ejecución", [["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", CLUSTER, "ServiceName", s] for s in ("reco-service", "rank-service")], stat="Maximum", y=6, x=12),
        widget("SageMaker: latencia del modelo p95 (µs)", [["AWS/SageMaker", "ModelLatency", "EndpointName", "reco-als", "VariantName", "AllTraffic"],
                                                          [".", "OverheadLatency", ".", ".", ".", "."]], y=12, x=0),
        widget("SageMaker: invocaciones por minuto", [["AWS/SageMaker", "Invocations", "EndpointName", "reco-als", "VariantName", "AllTraffic"]], stat="Sum", y=12, x=12),
    ]}
    cw.put_dashboard(DashboardName=f"{P}-operacion", DashboardBody=json.dumps(body))
    print(f"5 alarmas + filtro de logs + dashboard '{P}-operacion' creados; tópico SNS {P}-alerts")


def metrics(start: datetime, end: datetime, out: Path) -> None:
    """Resumen por minuto de una ventana: latencia ALB, peticiones, 5xx, CPU y tareas de ECS, SageMaker."""
    cw = boto3.client("cloudwatch")

    def series(namespace, metric, dims, stat=None, ext=None, scale=1.0):
        kwargs = {"ExtendedStatistics": ext} if ext else {"Statistics": [stat]}
        pts = cw.get_metric_statistics(Namespace=namespace, MetricName=metric, Dimensions=dims, StartTime=start, EndTime=end, Period=60, **kwargs)["Datapoints"]
        rows = {}
        for p in pts:
            key = p["Timestamp"].strftime("%H:%M")
            rows[key] = {k: round(v * scale, 1) for k, v in p["ExtendedStatistics"].items()} if ext else round(p[stat] * scale, 1)
        return dict(sorted(rows.items()))

    result = {
        "window_utc": [start.isoformat(), end.isoformat()],
        "alb_latency_ms": series("AWS/ApplicationELB", "TargetResponseTime", dims_alb(), ext=["p50", "p95", "p99"], scale=1000),
        "alb_requests_per_min": series("AWS/ApplicationELB", "RequestCount", dims_alb(), stat="Sum"),
        "alb_5xx_per_min": series("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dims_alb(), stat="Sum"),
        "cpu_reco_pct": series("AWS/ECS", "CPUUtilization", [{"Name": "ClusterName", "Value": CLUSTER}, {"Name": "ServiceName", "Value": "reco-service"}], stat="Average"),
        "cpu_rank_pct": series("AWS/ECS", "CPUUtilization", [{"Name": "ClusterName", "Value": CLUSTER}, {"Name": "ServiceName", "Value": "rank-service"}], stat="Average"),
        "tasks_reco": series("ECS/ContainerInsights", "RunningTaskCount", [{"Name": "ClusterName", "Value": CLUSTER}, {"Name": "ServiceName", "Value": "reco-service"}], stat="Maximum"),
        "tasks_rank": series("ECS/ContainerInsights", "RunningTaskCount", [{"Name": "ClusterName", "Value": CLUSTER}, {"Name": "ServiceName", "Value": "rank-service"}], stat="Maximum"),
        "sagemaker_model_latency_ms": series("AWS/SageMaker", "ModelLatency", SAGEMAKER_DIMS, ext=["p50", "p95", "p99"], scale=0.001),
        "sagemaker_invocations_per_min": series("AWS/SageMaker", "Invocations", SAGEMAKER_DIMS, stat="Sum"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "metrics"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    else:
        parse = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
        metrics(parse(args.start), parse(args.end), args.out)


if __name__ == "__main__":
    main()
