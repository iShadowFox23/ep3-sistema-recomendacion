---
name: apagar-demo
description: Apaga el stack de la demo EP3 después de presentar o ensayar (demo.py teardown). Usar cuando el usuario diga "apaga la demo", "terminamos", "apaga el sistema" o similar.
---

# Apagar la demo EP3

Proyecto en `C:\Users\lizbe\Proyectos\ep3-sistema-recomendacion`. Antes de correr scripts, poner `$env:PYTHONIOENCODING='utf-8'`.

1. Validar las credenciales: `.venv\Scripts\python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"`. Si expiraron (duran ~4 h): abrir `notepad "$env:USERPROFILE\.aws\credentials"` y pedirle al usuario que haga Start Lab → AWS Details → AWS CLI: Show, pegue el bloque y guarde. **Nunca** pedir las credenciales en el chat.
2. `.venv\Scripts\python infra\scripts\demo.py teardown`. Apaga las tareas ECS y deja el endpoint en máximo 1 instancia. Queda en ≈ USD 0,19/h por los balanceadores y el endpoint.
3. Preguntar (no hacerlo sin un sí explícito) si ya no se usará más el sistema. Solo en ese caso: `.venv\Scripts\python infra\scripts\sagemaker_endpoint.py delete`, que borra el endpoint (≈ USD 0,12/h) y para volver a usarlo hay que recrearlo.
4. Recordarle que revise el saldo en el panel del Learner Lab.
