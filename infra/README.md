# Infraestructura (AWS Academy Learner Lab)

Restricciones clave: solo `LabRole` (no se crean roles), SageMaker ml.t3/m5/c5 hasta xlarge,
push a ECR con las credenciales de usuario del lab, sin dominio propio (TLS vía URL de API Gateway).

Topología objetivo: API Gateway (REST, API keys + usage plans) → VPC Link → NLB → ALB → ECS Fargate,
y `rank-service` → endpoint real-time de SageMaker.

## Credenciales

En el Learner Lab: **Start Lab** → **AWS Details** → **AWS CLI: Show** y pegar el bloque en
`%USERPROFILE%\.aws\credentials` (región en `%USERPROFILE%\.aws\config`: `us-east-1`).
Expiran con la sesión del lab (~4 h): renovarlas en cada sesión. **Nunca** se suben al repo.

## Endpoint de SageMaker — `scripts/sagemaker_endpoint.py`

```bash
pip install boto3
python ml/train.py --splits train,valid --model-dir models/als        # genera el artefacto
python infra/scripts/sagemaker_endpoint.py package upload deploy autoscale test metrics
python infra/scripts/sagemaker_endpoint.py status
python infra/scripts/sagemaker_endpoint.py delete                      # al terminar la sesión
```

| Elemento | Configuración |
|---|---|
| Contenedor | SageMaker scikit-learn 1.2-1 (numpy/scipy/pandas); handlers en `code/inference.py` |
| Artefacto | `s3://reco-ep3-<cuenta>-us-east-1/models/reco-als/model.tar.gz` (bucket privado, cifrado SSE) |
| Endpoint | `reco-als`, variante `AllTraffic`, `ml.m5.large`, rol `LabRole` |
| Auto-scaling | 1–2 instancias, target tracking `SageMakerVariantInvocationsPerInstance` = 600/min |
| Actualización | `deploy` sobre un endpoint existente crea una nueva config y hace `update_endpoint` (sin downtime) |

### Evidencia del primer despliegue (2026-11-22)

- Creación del endpoint: ≈3,5 min hasta `InService`.
- Las 4 estrategias responden en el endpoint real: `als`, `item_similarity`, `popular_category`, `popular`.
- 100 peticiones desde el PC del equipo (Chile → us-east-1): p50 145 ms · p95 193 ms · p99 215 ms
  (incluye la red internet; desde ECS en la misma región la red aporta pocos ms).
- CloudWatch (104 invocaciones, 0 errores): `ModelLatency` p50 3,2 ms · p95 21,8 ms · p99 43,1 ms;
  `OverheadLatency` p50 6,4 ms · p95 57 ms (incluye las primeras invocaciones en frío).

Costo: `ml.m5.large` ≈ USD 0,12/h del crédito del lab. Borrar el endpoint al terminar cada sesión.

## Microservicios en ECS Fargate — `scripts/ecs_deploy.py`

```bash
python infra/scripts/ecs_deploy.py up        # crea/actualiza todo (idempotente, ~12 min la primera vez)
python infra/scripts/ecs_deploy.py status    # URLs y estado
python infra/scripts/ecs_deploy.py images    # reconstruye imágenes y redespliega
python infra/scripts/ecs_deploy.py scale 0   # apaga las tareas (conserva balanceadores y API)
python infra/scripts/ecs_deploy.py down      # borra lo creado por el script
```

```
Internet ──► ALB público :80 ──► dashboard (/)  ·  reco-service (/v1/recommendations, X-Api-Key)
Clientes ──► API Gateway REST /prod (API keys + usage plans) ──► VPC Link ──► NLB interno :80
              ──► ALB interno :80 ──► business-api · reco-service · rank-service ──► SageMaker reco-als
business-api / report-worker ──► NLB :5672 ──► rabbitmq
```

| Servicio | Fargate | Expuesto en |
|---|---|---|
| reco-service | 0,25 vCPU / 512 MB, auto-scaling 1–4 (CPU > 60%) | ALB público y ALB interno |
| rank-service | 0,25 vCPU / 512 MB, auto-scaling 1–3 | ALB interno |
| business-api | 0,5 vCPU / 1 GB | ALB interno ← NLB ← API Gateway |
| report-worker | 0,5 vCPU / 1 GB (misma imagen, `python -m src.worker`) | — |
| rabbitmq | 0,25 vCPU / 1 GB (`rabbitmq:3.13-management`) | NLB :5672 (solo VPC) |
| dashboard | 0,25 vCPU / 512 MB | ALB público |

Seguridad: secreto `reco-ep3/app` en Secrets Manager (contraseña de RabbitMQ y API keys, inyectadas
como variables por ECS); security groups que solo aceptan tráfico desde los balanceadores o la VPC;
API Gateway expone únicamente las rutas de business-api (`/v1/rank` no es accesible); usage plans
interno (≈100 req/min) y partner (≈20 req/min, 2.000/día); despliegues rolling 100/200 con circuit
breaker y rollback automático; logs en CloudWatch `/ecs/reco-ep3` (30 días) y Container Insights.

Limitaciones del lab (y qué se haría en producción): VPC por defecto con IP pública en las tareas
para salir a internet (producción: subredes privadas + VPC endpoints/NAT); ALB público por HTTP sin
dominio (producción: HTTPS con ACM + WAF); sin Cloud Map (se usa el ALB interno); caché en memoria
(producción: ElastiCache); RabbitMQ sin volumen persistente (producción: Amazon MQ); todo con LabRole.

### Evidencia del despliegue (2026-11-22)

- 6 servicios `ACTIVE` con 1 tarea cada uno; todos los target groups `healthy` (el NLB→ALB tarda
  ~3 min más que el resto en pasar a healthy: 5 chequeos de 30 s).
- Widget (ALB público): 200 usuarios distintos sin caché → 200 respuestas `als`, 0 en respaldo con el
  timeout de 150 ms. Latencia del lado del servidor (reco → rank → SageMaker):
  `x-response-time-ms` p50 30 · p95 38 · p99 94 ms; CloudWatch ALB `TargetResponseTime` p95 36–40 ms.
- API Gateway: sin key → 403; predict 200 (`als` e `item_similarity`); KPIs vía Athena ≈5,6 s
  (1.ª consulta, luego caché); reporte `top_products` listo en 5 s con URL prefirmada SigV4;
  ráfaga de 12 peticiones con el plan partner → 6 × 200 y 6 × 429; reporte con falla forzada →
  3 intentos → `failed` (mensaje en `reports.dlq`).
- Costo aproximado del stack encendido: ≈ USD 0,20/h + endpoint de SageMaker ≈ USD 0,12/h.
