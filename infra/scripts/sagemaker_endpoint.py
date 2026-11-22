"""Despliegue del modelo ALS como endpoint real-time de SageMaker (AWS Academy Learner Lab).

Usa boto3 con las credenciales de ~/.aws/credentials (se renuevan en cada sesión del lab).
Todos los recursos usan el rol preexistente LabRole (en el lab no se pueden crear roles).

Subcomandos:
    package    empaqueta models/als + código de inferencia en build/model.tar.gz
    upload     crea el bucket (si no existe) y sube model.tar.gz a S3
    deploy     crea Model, EndpointConfig y Endpoint; espera a InService
    autoscale  registra auto-scaling (1-2 instancias, target tracking por invocaciones)
    test       invoca el endpoint con peticiones de ejemplo y mide latencia
    metrics    latencia del servidor (ModelLatency/OverheadLatency), invocaciones y errores en CloudWatch
    status     estado del endpoint
    delete     borra endpoint, config, modelo y auto-scaling (el bucket S3 se conserva)

Ejemplo (desde la raíz del repo):
    python infra/scripts/sagemaker_endpoint.py package upload deploy autoscale test
    python infra/scripts/sagemaker_endpoint.py delete
"""
from __future__ import annotations

import argparse
import json
import statistics
import tarfile
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "als"
BUILD = ROOT / "build"
TARBALL = BUILD / "model.tar.gz"

NAME = "reco-als"
PREFIX = "models/reco-als"
INSTANCE_TYPE = "ml.m5.large"  # permitida en Learner Lab; 2 vCPU / 8 GB
# Contenedor oficial de SageMaker scikit-learn (trae numpy, scipy y pandas).
SKLEARN_IMAGE = {
    "us-east-1": "683313688378.dkr.ecr.us-east-1.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3",
    "us-west-2": "246618743249.dkr.ecr.us-west-2.amazonaws.com/sagemaker-scikit-learn:1.2-1-cpu-py3",
}


class Ctx:
    def __init__(self, region: str | None):
        self.session = boto3.Session(region_name=region)
        self.region = self.session.region_name
        self.account = self.session.client("sts").get_caller_identity()["Account"]
        self.role_arn = f"arn:aws:iam::{self.account}:role/LabRole"
        self.bucket = f"reco-ep3-{self.account}-{self.region}"
        self.sm = self.session.client("sagemaker")
        self.s3 = self.session.client("s3")
        self.aas = self.session.client("application-autoscaling")
        self.runtime = self.session.client("sagemaker-runtime")

    @property
    def model_data_url(self) -> str:
        return f"s3://{self.bucket}/{PREFIX}/model.tar.gz"


def package(_: Ctx) -> None:
    BUILD.mkdir(exist_ok=True)
    with tarfile.open(TARBALL, "w:gz") as tar:
        for name in ("model.npz", "metadata.json"):
            tar.add(MODEL_DIR / name, arcname=name)
        tar.add(ROOT / "ml" / "inference" / "inference.py", arcname="code/inference.py")
        tar.add(ROOT / "ml" / "recsys.py", arcname="code/recsys.py")
    print(f"package: {TARBALL.relative_to(ROOT)} ({TARBALL.stat().st_size / 1e6:.1f} MB)")


def upload(ctx: Ctx) -> None:
    try:
        ctx.s3.head_bucket(Bucket=ctx.bucket)
    except ClientError:
        kwargs = {} if ctx.region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": ctx.region}}
        ctx.s3.create_bucket(Bucket=ctx.bucket, **kwargs)
        ctx.s3.put_public_access_block(
            Bucket=ctx.bucket,
            PublicAccessBlockConfiguration={k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")},
        )
        ctx.s3.put_bucket_encryption(
            Bucket=ctx.bucket,
            ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
        )
        print(f"upload: bucket creado (privado, cifrado): {ctx.bucket}")
    ctx.s3.upload_file(str(TARBALL), ctx.bucket, f"{PREFIX}/model.tar.gz")
    print(f"upload: {ctx.model_data_url}")


def deploy(ctx: Ctx) -> None:
    version = time.strftime("%Y%m%d-%H%M%S")
    model_name, config_name = f"{NAME}-{version}", f"{NAME}-config-{version}"
    ctx.sm.create_model(
        ModelName=model_name,
        ExecutionRoleArn=ctx.role_arn,
        PrimaryContainer={
            "Image": SKLEARN_IMAGE[ctx.region],
            "ModelDataUrl": ctx.model_data_url,
            "Environment": {
                "SAGEMAKER_PROGRAM": "inference.py",
                "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
                "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
                "SAGEMAKER_REGION": ctx.region,
            },
        },
        Tags=[{"Key": "project", "Value": "ep3-reco"}],
    )
    ctx.sm.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": model_name,
                "InstanceType": INSTANCE_TYPE,
                "InitialInstanceCount": 1,
            }
        ],
    )
    try:
        ctx.sm.describe_endpoint(EndpointName=NAME)
        ctx.sm.update_endpoint(EndpointName=NAME, EndpointConfigName=config_name)
        print(f"deploy: actualizando endpoint {NAME} -> {model_name} (sin downtime)")
    except ClientError:
        ctx.sm.create_endpoint(EndpointName=NAME, EndpointConfigName=config_name)
        print(f"deploy: creando endpoint {NAME} con {model_name} en {INSTANCE_TYPE}")
    wait_in_service(ctx)


def wait_in_service(ctx: Ctx) -> None:
    start = time.time()
    while True:
        desc = ctx.sm.describe_endpoint(EndpointName=NAME)
        status = desc["EndpointStatus"]
        print(f"  {int(time.time() - start):>4}s  {status}", flush=True)
        if status == "InService":
            return
        if status == "Failed":
            raise SystemExit(f"endpoint falló: {desc.get('FailureReason')}")
        time.sleep(30)


def autoscale(ctx: Ctx) -> None:
    resource = f"endpoint/{NAME}/variant/AllTraffic"
    ctx.aas.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=resource,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        MinCapacity=1,
        MaxCapacity=2,  # tope bajo a propósito: presupuesto y límites del lab
    )
    ctx.aas.put_scaling_policy(
        PolicyName=f"{NAME}-invocations",
        ServiceNamespace="sagemaker",
        ResourceId=resource,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": 600.0,  # invocaciones por instancia por minuto
            "PredefinedMetricSpecification": {"PredefinedMetricType": "SageMakerVariantInvocationsPerInstance"},
            "ScaleOutCooldown": 60,
            "ScaleInCooldown": 300,
        },
    )
    print(f"autoscale: {resource} min=1 max=2, objetivo 600 invocaciones/instancia/min")


def test(ctx: Ctx) -> None:
    samples = [
        {"user_id": 257597, "context": "home", "k": 5},
        {"context": "pdp", "recent_items": [355908], "k": 5},
        {"user_id": 999999999, "category_id": 1532, "k": 5},
        {"k": 5},
    ]
    for body in samples:
        out = invoke(ctx, body)
        first = out["items"][0] if out["items"] else None
        print(f"test: {json.dumps(body)} -> strategy={out['strategy']} cold_start={out['cold_start']} primero={json.dumps(first)}")

    meta = json.loads((MODEL_DIR / "metadata.json").read_text(encoding="utf-8"))
    print(f"test: midiendo latencia cliente->endpoint (100 peticiones, modelo con {meta['n_users']} usuarios)")
    latencies = []
    for i in range(100):
        start = time.perf_counter()
        invoke(ctx, {"user_id": 257597 + i, "k": 10})
        latencies.append((time.perf_counter() - start) * 1000)
    q = statistics.quantiles(latencies, n=100)
    print(f"test: latencia cliente ms  p50={q[49]:.1f}  p95={q[94]:.1f}  p99={q[98]:.1f}  (incluye red desde el PC)")


def invoke(ctx: Ctx, body: dict) -> dict:
    resp = ctx.runtime.invoke_endpoint(
        EndpointName=NAME, ContentType="application/json", Accept="application/json", Body=json.dumps(body)
    )
    return json.loads(resp["Body"].read())


def metrics(ctx: Ctx) -> None:
    """Latencia del lado del servidor (CloudWatch, última hora con datos). ModelLatency = tiempo del
    contenedor; OverheadLatency = lo que agrega SageMaker. Vienen en microsegundos."""
    from datetime import datetime, timedelta, timezone

    cw = ctx.session.client("cloudwatch")
    end = datetime.now(timezone.utc)
    dims = [{"Name": "EndpointName", "Value": NAME}, {"Name": "VariantName", "Value": "AllTraffic"}]
    # Periodo de 1 minuto: con periodos de 1 h CloudWatch alinea a la hora y puede no devolver datos.
    for metric in ("ModelLatency", "OverheadLatency"):
        resp = cw.get_metric_statistics(
            Namespace="AWS/SageMaker", MetricName=metric, Dimensions=dims,
            StartTime=end - timedelta(hours=2), EndTime=end, Period=60,
            ExtendedStatistics=["p50", "p95", "p99"],
        )
        points = sorted(resp["Datapoints"], key=lambda p: p["Timestamp"])
        if not points:
            print(f"metrics: {metric} sin datos aún (CloudWatch tarda 1-2 min)")
            continue
        worst = max(points, key=lambda p: p["ExtendedStatistics"]["p95"])["ExtendedStatistics"]
        print(
            f"metrics: {metric:<16} ms  peor minuto: p50={worst['p50'] / 1000:.1f}  "
            f"p95={worst['p95'] / 1000:.1f}  p99={worst['p99'] / 1000:.1f}  ({len(points)} min con datos)"
        )
    inv = cw.get_metric_statistics(
        Namespace="AWS/SageMaker", MetricName="Invocations", Dimensions=dims,
        StartTime=end - timedelta(hours=2), EndTime=end, Period=60, Statistics=["Sum"],
    )["Datapoints"]
    errors = sum(
        p["Sum"]
        for m in ("Invocation4XXErrors", "Invocation5XXErrors")
        for p in cw.get_metric_statistics(
            Namespace="AWS/SageMaker", MetricName=m, Dimensions=dims,
            StartTime=end - timedelta(hours=2), EndTime=end, Period=60, Statistics=["Sum"],
        )["Datapoints"]
    )
    print(f"metrics: invocaciones={int(sum(p['Sum'] for p in inv))}  errores={int(errors)}  (últimas 2 h)")


def status(ctx: Ctx) -> None:
    try:
        desc = ctx.sm.describe_endpoint(EndpointName=NAME)
    except ClientError:
        print("status: no existe el endpoint")
        return
    variant = desc.get("ProductionVariants", [{}])[0]
    print(
        f"status: {desc['EndpointStatus']} | config={desc['EndpointConfigName']} | "
        f"instancias={variant.get('CurrentInstanceCount')} | creado={desc['CreationTime']:%Y-%m-%d %H:%M}"
    )


def delete(ctx: Ctx) -> None:
    resource = f"endpoint/{NAME}/variant/AllTraffic"
    try:
        ctx.aas.deregister_scalable_target(
            ServiceNamespace="sagemaker", ResourceId=resource, ScalableDimension="sagemaker:variant:DesiredInstanceCount"
        )
        print("delete: auto-scaling eliminado")
    except ClientError:
        pass
    try:
        ctx.sm.delete_endpoint(EndpointName=NAME)
        print(f"delete: endpoint {NAME} eliminado")
    except ClientError:
        print("delete: no había endpoint")
    for cfg in ctx.sm.list_endpoint_configs(NameContains=NAME)["EndpointConfigs"]:
        ctx.sm.delete_endpoint_config(EndpointConfigName=cfg["EndpointConfigName"])
        print(f"delete: config {cfg['EndpointConfigName']}")
    for model in ctx.sm.list_models(NameContains=NAME)["Models"]:
        ctx.sm.delete_model(ModelName=model["ModelName"])
        print(f"delete: modelo {model['ModelName']}")
    print(f"delete: el artefacto sigue en s3://{ctx.bucket}/{PREFIX}/ (costo mínimo)")


COMMANDS = {"package": package, "upload": upload, "deploy": deploy, "autoscale": autoscale, "test": test, "metrics": metrics, "status": status, "delete": delete}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commands", nargs="+", choices=list(COMMANDS))
    parser.add_argument("--region", default=None)
    args = parser.parse_args()
    ctx = Ctx(args.region)
    for command in args.commands:
        COMMANDS[command](ctx)


if __name__ == "__main__":
    main()
