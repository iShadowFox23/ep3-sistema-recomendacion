"""Despliegue de los microservicios en ECS Fargate (AWS Academy Learner Lab), con boto3.

Topología:

    Internet ──► ALB público (HTTP :80) ──► dashboard (/)            [Streamlit]
                                       └──► reco-service (/v1/recommendations)   [widget Shopify]

    Clientes empresariales ──► API Gateway REST (/prod, API keys + usage plans)
         └──► VPC Link ──► NLB interno :80 ──► ALB interno :80 ──► business-api
                                                        ├──► reco-service  (/v1/recommendations, llamado por business-api)
                                                        └──► rank-service  (/v1/rank, llamado por reco-service) ──► SageMaker reco-als
    business-api / report-worker ──► NLB interno :5672 ──► rabbitmq

Decisiones por restricciones del lab (documentadas en infra/README.md):
- VPC por defecto (subredes en us-east-1a y 1b). Las tareas tienen IP pública solo para salir a
  internet (ECR, Docker Hub, APIs de AWS) sin NAT Gateway ni VPC endpoints; los security groups
  solo aceptan tráfico entrante desde los balanceadores o desde la VPC.
- Sin AWS Cloud Map (no habilitado): la comunicación interna pasa por el ALB interno y el NLB.
- Sin dominio propio: el ALB público usa HTTP; el TLS está en la URL HTTPS de API Gateway.
- Sin ElastiCache (costo): reco-service usa caché en memoria. RabbitMQ en Fargate sin volumen
  persistente (aceptable en la demo; en producción, Amazon MQ).
- Todos los roles = LabRole.

Uso (desde la raíz del repo, con credenciales del lab en ~/.aws):
    python infra/scripts/ecs_deploy.py up          # crea o actualiza todo (idempotente)
    python infra/scripts/ecs_deploy.py images      # solo reconstruye y publica imágenes + redeploy
    python infra/scripts/ecs_deploy.py images --only reco-service   # un solo servicio
    python infra/scripts/ecs_deploy.py status      # URLs y estado de los servicios
    python infra/scripts/ecs_deploy.py scale 0     # apaga las tareas (conserva la infraestructura)
    python infra/scripts/ecs_deploy.py scale 1     # las vuelve a encender
    python infra/scripts/ecs_deploy.py down        # borra todo lo creado por este script
El estado (IDs, URLs) se guarda en build/aws_state.json; los secretos solo en Secrets Manager.
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / "build" / "aws_state.json"
P = "reco-ep3"  # prefijo de todos los recursos
CLUSTER = f"{P}-cluster"
LOG_GROUP = f"/ecs/{P}"
SECRET_NAME = f"{P}/app"
AZS = ("us-east-1a", "us-east-1b")
TAGS = [{"Key": "project", "Value": P}]

AUTOSCALED = {"reco-service": (2, 6), "rank-service": (2, 4)}  # (mínimo, máximo) de tareas
IMAGES = {  # repositorio ECR -> carpeta con el Dockerfile
    "reco-service": "services/reco-service",
    "rank-service": "services/rank-service",
    "business-api": "services/business-api",
    "dashboard": "dashboard",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Aws:
    def __init__(self):
        self.s = boto3.Session()
        self.region = self.s.region_name
        self.account = self.s.client("sts").get_caller_identity()["Account"]
        self.role = f"arn:aws:iam::{self.account}:role/LabRole"
        self.bucket = f"reco-ep3-{self.account}-{self.region}"
        for name in ("ec2", "ecs", "ecr", "elbv2", "logs", "secretsmanager", "apigateway", "application-autoscaling", "s3"):
            setattr(self, name.replace("-", "_"), self.s.client(name))
        self.state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    def save(self) -> None:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, indent=2))


# ============================================================================ red y seguridad


def network(a: Aws) -> None:
    vpc = a.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    subnets = a.ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}, {"Name": "availability-zone", "Values": list(AZS)}]
    )["Subnets"]
    a.state.update(vpc_id=vpc["VpcId"], vpc_cidr=vpc["CidrBlock"], subnets=[s["SubnetId"] for s in subnets])

    def sg(name: str, desc: str, rules: list[dict]) -> str:
        found = a.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc["VpcId"]]}]
        )["SecurityGroups"]
        sg_id = found[0]["GroupId"] if found else a.ec2.create_security_group(
            GroupName=name, Description=desc, VpcId=vpc["VpcId"],
            TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}],
        )["GroupId"]
        for rule in rules:
            try:
                a.ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[rule])
            except ClientError as exc:
                if "Duplicate" not in str(exc):
                    raise
        return sg_id

    cidr = vpc["CidrBlock"]
    alb_pub = sg(f"{P}-alb-public", "ALB publico: HTTP desde internet", [
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    ])
    alb_int = sg(f"{P}-alb-internal", "ALB interno: solo desde la VPC (NLB y tareas)", [
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": cidr}]},
    ])
    tasks = sg(f"{P}-tasks", "Tareas ECS: solo desde los ALB y RabbitMQ desde la VPC", [
        {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "UserIdGroupPairs": [{"GroupId": alb_pub}, {"GroupId": alb_int}]},
        {"IpProtocol": "tcp", "FromPort": 8501, "ToPort": 8501, "UserIdGroupPairs": [{"GroupId": alb_pub}]},
        {"IpProtocol": "tcp", "FromPort": 5672, "ToPort": 5672, "IpRanges": [{"CidrIp": cidr}]},
    ])
    a.state.update(sg_alb_public=alb_pub, sg_alb_internal=alb_int, sg_tasks=tasks)
    log(f"red: VPC por defecto, subredes {AZS}, 3 security groups")


# ============================================================================ imágenes


def images(a: Aws, only: set[str] | None = None) -> None:
    token = a.ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    registry = token["proxyEndpoint"].replace("https://", "")
    subprocess.run(["docker", "login", "-u", user, "--password-stdin", registry], input=password.encode(), check=True, capture_output=True)
    tag = time.strftime("v%Y%m%d-%H%M%S")
    a.state.setdefault("images", {})
    for repo, folder in IMAGES.items():
        if only and repo not in only:
            continue
        try:
            a.ecr.create_repository(repositoryName=f"{P}/{repo}", imageScanningConfiguration={"scanOnPush": True}, tags=TAGS)
        except a.ecr.exceptions.RepositoryAlreadyExistsException:
            pass
        uri = f"{registry}/{P}/{repo}:{tag}"
        log(f"imagen {repo}: build + push {tag}")
        for cmd in (["docker", "build", "--platform", "linux/amd64", "-t", uri, str(ROOT / folder)], ["docker", "push", uri]):
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                sys.exit(f"falló {' '.join(cmd[:2])} de {repo}: {result.stderr[-2000:]}")
        a.state["images"][repo] = uri
    a.save()


# ============================================================================ secretos, logs, cluster


def secret_and_logs(a: Aws) -> None:
    try:
        arn = a.secretsmanager.describe_secret(SecretId=SECRET_NAME)["ARN"]
    except a.secretsmanager.exceptions.ResourceNotFoundException:
        rabbit_pass = secrets.token_urlsafe(24)
        widget_key, internal_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        value = {
            "RABBITMQ_USER": "reco",
            "RABBITMQ_PASS": rabbit_pass,
            "WIDGET_API_KEY": widget_key,  # la usa el widget de Shopify contra reco-service
            "INTERNAL_API_KEY": internal_key,  # la usa business-api contra reco-service
            "API_KEYS": f"{widget_key},{internal_key}",
            "DASHBOARD_API_KEY": secrets.token_urlsafe(30),  # API key de API Gateway del dashboard (plan interno)
            "PARTNER_API_KEY": secrets.token_urlsafe(30),  # API key de ejemplo para un cliente externo (plan partner)
        }
        arn = a.secretsmanager.create_secret(
            Name=SECRET_NAME, SecretString=json.dumps(value), Tags=TAGS,
            Description="Credenciales de la app reco-ep3 (RabbitMQ, API keys)",
        )["ARN"]
        log("secreto creado en Secrets Manager (valores generados, no se muestran)")
    a.state["secret_arn"] = arn
    secret = json.loads(a.secretsmanager.get_secret_value(SecretId=arn)["SecretString"])
    if "RABBITMQ_URL" not in secret:  # depende del DNS del NLB: se completa después de crearlo
        a.state["_needs_rabbit_url"] = True
    try:
        a.logs.create_log_group(logGroupName=LOG_GROUP, tags={"project": P})
        a.logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
    except a.logs.exceptions.ResourceAlreadyExistsException:
        pass
    for attempt in range(3):  # el rol vinculado de ECS puede no existir aún la primera vez
        try:
            a.ecs.create_cluster(clusterName=CLUSTER, settings=[{"name": "containerInsights", "value": "enabled"}], tags=[{"key": "project", "value": P}])
            break
        except ClientError as exc:
            if attempt == 2:
                raise
            log(f"cluster: reintento ({exc.response['Error']['Code']})")
            time.sleep(10)
    log(f"cluster {CLUSTER} con Container Insights; logs en {LOG_GROUP} (30 días)")


# ============================================================================ balanceadores


def load_balancers(a: Aws) -> None:
    def lb(name: str, scheme: str, kind: str, sgs: list[str] | None):
        try:
            return a.elbv2.describe_load_balancers(Names=[name])["LoadBalancers"][0]
        except ClientError:
            kwargs = {"SecurityGroups": sgs} if sgs else {}
            return a.elbv2.create_load_balancer(
                Name=name, Subnets=a.state["subnets"], Scheme=scheme, Type=kind, Tags=TAGS, **kwargs
            )["LoadBalancers"][0]

    pub = lb(f"{P}-public", "internet-facing", "application", [a.state["sg_alb_public"]])
    internal = lb(f"{P}-internal", "internal", "application", [a.state["sg_alb_internal"]])
    nlb = lb(f"{P}-nlb", "internal", "network", None)
    a.state.update(alb_public=pub["LoadBalancerArn"], alb_public_dns=pub["DNSName"],
                   alb_internal=internal["LoadBalancerArn"], alb_internal_dns=internal["DNSName"],
                   nlb=nlb["LoadBalancerArn"], nlb_dns=nlb["DNSName"])
    log("balanceadores creados; esperando a que estén activos (2-4 min)")
    a.elbv2.get_waiter("load_balancer_available").wait(LoadBalancerArns=[pub["LoadBalancerArn"], internal["LoadBalancerArn"], nlb["LoadBalancerArn"]])

    def tg(name: str, port: int, health: str, protocol: str = "HTTP", target_type: str = "ip") -> str:
        try:
            return a.elbv2.describe_target_groups(Names=[name])["TargetGroups"][0]["TargetGroupArn"]
        except ClientError:
            kwargs = dict(Name=name, Protocol=protocol, Port=port, VpcId=a.state["vpc_id"], TargetType=target_type, Tags=TAGS)
            if protocol == "HTTP":
                kwargs.update(HealthCheckPath=health, HealthCheckIntervalSeconds=15, HealthyThresholdCount=2, UnhealthyThresholdCount=2, Matcher={"HttpCode": "200"})
            elif target_type == "alb":
                kwargs.update(HealthCheckProtocol="HTTP", HealthCheckPath=health)
            arn = a.elbv2.create_target_group(**kwargs)["TargetGroups"][0]["TargetGroupArn"]
            if target_type == "ip":
                a.elbv2.modify_target_group_attributes(TargetGroupArn=arn, Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}])
            return arn

    tgs = {
        "reco_pub": tg(f"{P}-reco-pub", 8080, "/health"),
        "dashboard": tg(f"{P}-dashboard", 8501, "/_stcore/health"),
        "reco_int": tg(f"{P}-reco-int", 8080, "/health"),
        "rank": tg(f"{P}-rank", 8080, "/health"),
        "business": tg(f"{P}-business", 8080, "/health"),
        "rabbit": tg(f"{P}-rabbit", 5672, "", protocol="TCP"),
        "nlb_alb": tg(f"{P}-nlb-to-alb", 80, "/health", protocol="TCP", target_type="alb"),
    }
    a.state["tg"] = tgs

    def listener(lb_arn: str, port: int, protocol: str, default_tg: str) -> str:
        existing = a.elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
        for item in existing:
            if item["Port"] == port:
                return item["ListenerArn"]
        return a.elbv2.create_listener(
            LoadBalancerArn=lb_arn, Port=port, Protocol=protocol,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": default_tg}],
        )["Listeners"][0]["ListenerArn"]

    def rules(listener_arn: str, routes: list[tuple[int, str, str]]) -> None:
        existing = {r["Priority"] for r in a.elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]}
        for priority, path, tg_arn in routes:
            if str(priority) not in existing:
                a.elbv2.create_rule(
                    ListenerArn=listener_arn, Priority=priority,
                    Conditions=[{"Field": "path-pattern", "Values": [path]}],
                    Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
                )

    pub_listener = listener(a.state["alb_public"], 80, "HTTP", tgs["dashboard"])
    rules(pub_listener, [(10, "/v1/recommendations", tgs["reco_pub"])])
    int_listener = listener(a.state["alb_internal"], 80, "HTTP", tgs["business"])
    rules(int_listener, [
        (10, "/v1/recommendations/predict", tgs["business"]),
        (20, "/v1/recommendations", tgs["reco_int"]),
        (30, "/v1/rank", tgs["rank"]),
    ])
    a.elbv2.register_targets(TargetGroupArn=tgs["nlb_alb"], Targets=[{"Id": a.state["alb_internal"], "Port": 80}])
    listener(a.state["nlb"], 80, "TCP", tgs["nlb_alb"])
    listener(a.state["nlb"], 5672, "TCP", tgs["rabbit"])

    # La URL de RabbitMQ depende del DNS del NLB: se guarda en el secreto (incluye la contraseña).
    a.state.pop("_needs_rabbit_url", None)
    secret = json.loads(a.secretsmanager.get_secret_value(SecretId=a.state["secret_arn"])["SecretString"])
    url = f"amqp://{secret['RABBITMQ_USER']}:{secret['RABBITMQ_PASS']}@{a.state['nlb_dns']}:5672/"
    if secret.get("RABBITMQ_URL") != url:
        secret["RABBITMQ_URL"] = url
        a.secretsmanager.put_secret_value(SecretId=a.state["secret_arn"], SecretString=json.dumps(secret))
    a.save()
    log("ALB público (dashboard + widget), ALB interno (business/reco/rank) y NLB (API Gateway + RabbitMQ) listos")


# ============================================================================ servicios ECS


def task_definitions(a: Aws) -> dict[str, dict]:
    st, sec, bucket, region = a.state, a.state["secret_arn"], a.bucket, a.region
    internal = f"http://{st['alb_internal_dns']}"

    def secret(key: str) -> dict:
        return {"name": key, "valueFrom": f"{sec}:{key}::"}

    common_env = [{"name": "AWS_REGION", "value": region}, {"name": "AWS_DEFAULT_REGION", "value": region}]
    business_env = common_env + [
        {"name": "RECO_SERVICE_URL", "value": internal},
        {"name": "ANALYTICS_BACKEND", "value": "athena"},
        {"name": "ATHENA_DATABASE", "value": "reco_ep3"},
        {"name": "ATHENA_OUTPUT", "value": f"s3://{bucket}/athena-results/"},
        {"name": "CATALOG_PATH", "value": f"s3://{bucket}/serving/catalog.csv"},
        {"name": "JOBS_TABLE", "value": "report_jobs"},
        {"name": "REPORTS_BUCKET", "value": bucket},
        {"name": "ROOT_PATH", "value": "/prod"},
        {"name": "ALLOW_FAULT_INJECTION", "value": "true"},  # demo de reintentos y DLQ
    ]
    business_secrets = [{"name": "RABBITMQ_URL", "valueFrom": f"{sec}:RABBITMQ_URL::"}, {"name": "RECO_API_KEY", "valueFrom": f"{sec}:INTERNAL_API_KEY::"}]
    return {
        "rabbitmq": dict(cpu=256, memory=1024, port=5672, image="rabbitmq:3.13-management", env=[],
                         secrets=[{"name": "RABBITMQ_DEFAULT_USER", "valueFrom": f"{sec}:RABBITMQ_USER::"},
                                  {"name": "RABBITMQ_DEFAULT_PASS", "valueFrom": f"{sec}:RABBITMQ_PASS::"}],
                         lbs=[("rabbit", 5672)]),
        "rank-service": dict(cpu=512, memory=1024, port=8080, image=st["images"]["rank-service"],
                             env=common_env + [
                                 {"name": "MODEL_BACKEND", "value": "sagemaker"},
                                 {"name": "SAGEMAKER_ENDPOINT", "value": "reco-als"},
                                 {"name": "MODEL_TIMEOUT_S", "value": "1.0"},
                                 {"name": "CATALOG_PATH", "value": f"s3://{bucket}/serving/catalog.csv"},
                             ], secrets=[], lbs=[("rank", 8080)]),
        "reco-service": dict(cpu=512, memory=1024, port=8080, image=st["images"]["reco-service"],
                             env=common_env + [
                                 {"name": "RANK_SERVICE_URL", "value": internal},
                                 {"name": "RANK_TIMEOUT_MS", "value": "150"},
                                 {"name": "POPULAR_PATH", "value": f"s3://{bucket}/serving/popular.json"},
                             ], secrets=[secret("API_KEYS")], lbs=[("reco_pub", 8080), ("reco_int", 8080)]),
        "business-api": dict(cpu=512, memory=1024, port=8080, image=st["images"]["business-api"],
                             env=business_env, secrets=business_secrets, lbs=[("business", 8080)]),
        "report-worker": dict(cpu=512, memory=1024, port=None, image=st["images"]["business-api"],
                              env=business_env, secrets=business_secrets, lbs=[], command=["python", "-m", "src.worker"]),
        "dashboard": dict(cpu=256, memory=512, port=8501, image=st["images"]["dashboard"],
                          env=common_env + [
                              {"name": "BUSINESS_API_URL", "value": st.get("api_url", "")},
                              {"name": "METRICS_PATH", "value": f"s3://{bucket}/serving/metrics_test.json"},
                          ], secrets=[{"name": "BUSINESS_API_KEY", "valueFrom": f"{sec}:DASHBOARD_API_KEY::"}],
                          lbs=[("dashboard", 8501)]),
    }


def services(a: Aws, only: set[str] | None = None) -> None:
    a.s3.upload_file(str(ROOT / "reports" / "metrics_test.json"), a.bucket, "serving/metrics_test.json")
    for name, spec in task_definitions(a).items():
        if only and name not in only:
            continue
        container = {
            "name": name,
            "image": spec["image"],
            "essential": True,
            "environment": spec["env"],
            "secrets": spec["secrets"],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": LOG_GROUP, "awslogs-region": a.region, "awslogs-stream-prefix": name}},
        }
        if spec["port"]:
            container["portMappings"] = [{"containerPort": spec["port"], "protocol": "tcp"}]
        if spec.get("command"):
            container["command"] = spec["command"]
        td = a.ecs.register_task_definition(
            family=f"{P}-{name}", networkMode="awsvpc", requiresCompatibilities=["FARGATE"],
            cpu=str(spec["cpu"]), memory=str(spec["memory"]), taskRoleArn=a.role, executionRoleArn=a.role,
            containerDefinitions=[container], runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        )["taskDefinition"]["taskDefinitionArn"]
        network_cfg = {"awsvpcConfiguration": {"subnets": a.state["subnets"], "securityGroups": [a.state["sg_tasks"]], "assignPublicIp": "ENABLED"}}
        existing = a.ecs.describe_services(cluster=CLUSTER, services=[name])["services"]
        if existing and existing[0]["status"] == "ACTIVE":
            a.ecs.update_service(cluster=CLUSTER, service=name, taskDefinition=td, forceNewDeployment=True)
            log(f"servicio {name}: actualizado ({td.split('/')[-1]})")
        else:
            lbs = [{"targetGroupArn": a.state["tg"][key], "containerName": name, "containerPort": port} for key, port in spec["lbs"]]
            kwargs = {"loadBalancers": lbs, "healthCheckGracePeriodSeconds": 60} if lbs else {}
            a.ecs.create_service(
                cluster=CLUSTER, serviceName=name, taskDefinition=td, desiredCount=1, launchType="FARGATE",
                networkConfiguration=network_cfg, propagateTags="SERVICE", enableExecuteCommand=False,
                deploymentConfiguration={"minimumHealthyPercent": 100, "maximumPercent": 200,
                                         "deploymentCircuitBreaker": {"enable": True, "rollback": True}},
                tags=[{"key": "project", "value": P}], **kwargs,
            )
            log(f"servicio {name}: creado")
    autoscaling(a)


def autoscaling(a: Aws) -> None:
    # Diseño EP2: 0,5 vCPU y mínimo 2 réplicas (una por AZ). La primera prueba de carga con 0,25 vCPU y
    # mínimo 1 saturó reco-service al 100% de CPU con 60 req/s antes de que el auto-scaling reaccionara.
    for name, (min_tasks, max_tasks) in AUTOSCALED.items():
        resource = f"service/{CLUSTER}/{name}"
        a.application_autoscaling.register_scalable_target(
            ServiceNamespace="ecs", ResourceId=resource, ScalableDimension="ecs:service:DesiredCount", MinCapacity=min_tasks, MaxCapacity=max_tasks)
        a.application_autoscaling.put_scaling_policy(
            PolicyName=f"{name}-cpu60", ServiceNamespace="ecs", ResourceId=resource, ScalableDimension="ecs:service:DesiredCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration={
                "TargetValue": 60.0, "ScaleOutCooldown": 60, "ScaleInCooldown": 180,
                "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"}},
        )
    log("auto-scaling: reco-service 2-6 y rank-service 2-4 tareas por CPU > 60%")


def wait_stable(a: Aws) -> None:
    names = list(task_definitions(a))
    log("esperando a que los servicios queden estables (3-6 min)")
    a.ecs.get_waiter("services_stable").wait(cluster=CLUSTER, services=names, WaiterConfig={"Delay": 15, "MaxAttempts": 60})
    log("servicios estables")


# ============================================================================ API Gateway


def api_gateway(a: Aws) -> None:
    gw, st = a.apigateway, a.state
    links = [l for l in gw.get_vpc_links()["items"] if l["name"] == f"{P}-link"]
    link_id = links[0]["id"] if links else gw.create_vpc_link(name=f"{P}-link", targetArns=[st["nlb"]], tags={"project": P})["id"]
    log("API Gateway: esperando VPC Link (puede tardar varios minutos)")
    while (status := gw.get_vpc_link(vpcLinkId=link_id)["status"]) == "PENDING":
        time.sleep(20)
    if status != "AVAILABLE":
        sys.exit(f"VPC Link en estado {status}")

    apis = [x for x in gw.get_rest_apis(limit=100)["items"] if x["name"] == f"{P}-business-api"]
    if apis:
        api_id = apis[0]["id"]
    else:
        api_id = gw.create_rest_api(
            name=f"{P}-business-api", description="API empresarial (business-api) vía VPC Link",
            endpointConfiguration={"types": ["REGIONAL"]}, apiKeySource="HEADER", tags={"project": P},
        )["id"]
        resources = {r["path"]: r["id"] for r in gw.get_resources(restApiId=api_id)["items"]}

        def resource(path: str) -> str:
            if path in resources:
                return resources[path]
            parent, part = path.rsplit("/", 1)
            resources[path] = gw.create_resource(restApiId=api_id, parentId=resource(parent or "/"), pathPart=part)["id"]
            return resources[path]

        # Solo se exponen las rutas de business-api; /v1/rank y /v1/recommendations del ALB interno quedan fuera.
        routes = [
            ("/v1/recommendations/predict", "POST", True),
            ("/v1/insights/{proxy+}", "GET", True),
            ("/v1/reports", "POST", True),
            ("/v1/reports/{proxy+}", "GET", True),
            ("/openapi.json", "GET", False),  # documentación pública (sin datos)
            ("/docs", "GET", False),
        ]
        for path, method, key_required in routes:
            rid = resource(path)
            proxy = "{proxy+}" in path
            backend_path = path.replace("{proxy+}", "{proxy}")
            gw.put_method(restApiId=api_id, resourceId=rid, httpMethod=method, authorizationType="NONE", apiKeyRequired=key_required,
                          requestParameters={"method.request.path.proxy": True} if proxy else {})
            params = {"integration.request.header.x-api-key-id": "context.identity.apiKeyId"}
            if proxy:
                params["integration.request.path.proxy"] = "method.request.path.proxy"
            gw.put_integration(
                restApiId=api_id, resourceId=rid, httpMethod=method, type="HTTP_PROXY", integrationHttpMethod=method,
                uri=f"http://{st['nlb_dns']}{backend_path}", connectionType="VPC_LINK", connectionId=link_id,
                requestParameters=params, timeoutInMillis=29000,
            )
        gw.create_deployment(restApiId=api_id, stageName="prod", description="despliegue inicial")
        gw.update_stage(restApiId=api_id, stageName="prod", patchOperations=[
            {"op": "replace", "path": "/*/*/metrics/enabled", "value": "true"},  # métricas detalladas en CloudWatch
        ])
        log("API Gateway: REST API, rutas, VPC Link y etapa prod creadas")

    secret = json.loads(a.secretsmanager.get_secret_value(SecretId=st["secret_arn"])["SecretString"])
    plans = {p["name"]: p["id"] for p in gw.get_usage_plans(limit=100)["items"]}
    # Límites por segundo: interno ≈ 100 req/min, partner ≈ 20 req/min con cuota diaria (diseño EP3).
    specs = {
        f"{P}-interno": ({"rateLimit": 1.7, "burstLimit": 20}, None, "DASHBOARD_API_KEY", "dashboard"),
        f"{P}-partner": ({"rateLimit": 0.34, "burstLimit": 5}, {"limit": 2000, "period": "DAY"}, "PARTNER_API_KEY", "partner-demo"),
    }
    existing_keys = {k["name"]: k["id"] for k in gw.get_api_keys(limit=100, includeValues=False)["items"]}
    for plan_name, (throttle, quota, secret_key, key_name) in specs.items():
        if plan_name not in plans:
            kwargs = {"quota": quota} if quota else {}
            plans[plan_name] = gw.create_usage_plan(
                name=plan_name, apiStages=[{"apiId": api_id, "stage": "prod"}], throttle=throttle, tags={"project": P}, **kwargs)["id"]
        full_key_name = f"{P}-{key_name}"
        if full_key_name not in existing_keys:
            key_id = gw.create_api_key(name=full_key_name, enabled=True, value=secret[secret_key], tags={"project": P})["id"]
            gw.create_usage_plan_key(usagePlanId=plans[plan_name], keyId=key_id, keyType="API_KEY")
    st.update(api_id=api_id, api_url=f"https://{api_id}.execute-api.{a.region}.amazonaws.com/prod", vpc_link=link_id)
    a.save()
    log(f"API Gateway: usage plans interno y partner con sus API keys; URL {st['api_url']}")


# ============================================================================ comandos


def status(a: Aws) -> None:
    st = a.state
    if not st:
        print("no hay despliegue registrado")
        return
    print(f"Dashboard:            http://{st.get('alb_public_dns')}/")
    print(f"Widget (reco-service): http://{st.get('alb_public_dns')}/v1/recommendations  (header X-Api-Key)")
    print(f"API empresarial:      {st.get('api_url')}  (header x-api-key; Swagger en /docs)")
    try:
        svcs = a.ecs.describe_services(cluster=CLUSTER, services=list(task_definitions(a)))["services"]
        for s in svcs:
            print(f"  {s['serviceName']:<14} {s['status']:<8} running={s['runningCount']} desired={s['desiredCount']}")
    except ClientError:
        pass


def scale(a: Aws, count: int) -> None:
    """Apaga (0) o enciende (>=1) todas las tareas. En los servicios con auto-scaling también se
    ajusta el mínimo; si no, Application Auto Scaling los volvería a subir a 1."""
    for name, (min_tasks, max_tasks) in AUTOSCALED.items():
        a.application_autoscaling.register_scalable_target(
            ServiceNamespace="ecs", ResourceId=f"service/{CLUSTER}/{name}", ScalableDimension="ecs:service:DesiredCount",
            MinCapacity=min_tasks if count else 0, MaxCapacity=max_tasks if count else 0,
        )
    for name in task_definitions(a):
        a.ecs.update_service(cluster=CLUSTER, service=name, desiredCount=count)
    log(f"servicios escalados a {count} tarea(s)")
    if count:
        wait_stable(a)
        log("ojo: el NLB tarda ~3 min extra en marcar sano al ALB interno; antes de eso API Gateway puede dar 500")


def down(a: Aws) -> None:
    st = a.state
    gw = a.apigateway
    for p in gw.get_usage_plans(limit=100)["items"]:
        if p["name"].startswith(P):
            for k in gw.get_usage_plan_keys(usagePlanId=p["id"])["items"]:
                gw.delete_usage_plan_key(usagePlanId=p["id"], keyId=k["id"])
            gw.update_usage_plan(usagePlanId=p["id"], patchOperations=[{"op": "remove", "path": "/apiStages", "value": f"{st.get('api_id')}:prod"}])
            gw.delete_usage_plan(usagePlanId=p["id"])
    for k in gw.get_api_keys(limit=100)["items"]:
        if k["name"].startswith(P):
            gw.delete_api_key(apiKey=k["id"])
    for api in gw.get_rest_apis(limit=100)["items"]:
        if api["name"] == f"{P}-business-api":
            gw.delete_rest_api(restApiId=api["id"])
    for link in gw.get_vpc_links()["items"]:
        if link["name"] == f"{P}-link":
            gw.delete_vpc_link(vpcLinkId=link["id"])
    log("API Gateway eliminado")
    for name in ("reco-service", "rank-service"):
        try:
            a.application_autoscaling.deregister_scalable_target(ServiceNamespace="ecs", ResourceId=f"service/{CLUSTER}/{name}", ScalableDimension="ecs:service:DesiredCount")
        except ClientError:
            pass
    try:
        names = [s.split("/")[-1] for s in a.ecs.list_services(cluster=CLUSTER)["serviceArns"]]
        for name in names:
            a.ecs.delete_service(cluster=CLUSTER, service=name, force=True)
        if names:
            a.ecs.get_waiter("services_inactive").wait(cluster=CLUSTER, services=names)
        a.ecs.delete_cluster(cluster=CLUSTER)
        log("servicios y cluster ECS eliminados")
    except ClientError:
        pass
    for lb_name in (f"{P}-public", f"{P}-internal", f"{P}-nlb"):
        try:
            arn = a.elbv2.describe_load_balancers(Names=[lb_name])["LoadBalancers"][0]["LoadBalancerArn"]
            a.elbv2.delete_load_balancer(LoadBalancerArn=arn)
        except ClientError:
            pass
    time.sleep(30)
    for tg in a.elbv2.describe_target_groups()["TargetGroups"]:
        if tg["TargetGroupName"].startswith(P):
            for _ in range(10):
                try:
                    a.elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                    break
                except ClientError:
                    time.sleep(15)
    log("balanceadores y target groups eliminados")
    for name in (f"{P}-tasks", f"{P}-alb-public", f"{P}-alb-internal"):
        for _ in range(20):  # las interfaces de red tardan en liberarse
            try:
                groups = a.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
                if groups:
                    a.ec2.delete_security_group(GroupId=groups[0]["GroupId"])
                break
            except ClientError:
                time.sleep(15)
    log("security groups eliminados")
    print("Se conservan: imágenes ECR, secreto, logs, bucket S3, DynamoDB y el endpoint de SageMaker (borrar aparte).")
    STATE_FILE.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "images", "status", "scale", "down"])
    parser.add_argument("count", nargs="?", type=int, default=1)
    parser.add_argument("--only", nargs="+", help="con 'images': servicios a reconstruir y redesplegar")
    args = parser.parse_args()
    a = Aws()
    if args.command == "up":
        network(a)
        a.save()
        images(a)
        secret_and_logs(a)
        load_balancers(a)
        api_gateway(a)  # antes de los servicios: el dashboard necesita la URL de la API
        services(a)
        wait_stable(a)
        status(a)
    elif args.command == "images":
        only = set(args.only) if args.only else None
        images(a, only)
        services(a, only)
        wait_stable(a)
    elif args.command == "status":
        status(a)
    elif args.command == "scale":
        scale(a, args.count)
    else:
        down(a)


if __name__ == "__main__":
    main()
