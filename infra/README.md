# Infraestructura (AWS Academy Learner Lab)

Restricciones clave: solo `LabRole` (no se crean roles), SageMaker ml.t3/m5/c5 hasta xlarge,
push a ECR con las credenciales de usuario del lab, sin dominio propio (TLS vía URL de API Gateway).

Topología: API Gateway (REST, API keys + usage plans) → VPC Link → NLB → ALB → ECS Fargate.

TODO: scripts de despliegue en `scripts/` y, si alcanza, plantillas CloudFormation.
Apagar/borrar el endpoint de SageMaker, NLB/ALB y tareas ECS después de cada sesión.
