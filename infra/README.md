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
