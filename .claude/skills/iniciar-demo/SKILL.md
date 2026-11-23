---
name: iniciar-demo
description: Deja lista la demo EP3 el día de la presentación, desde el portátil. Renueva credenciales, enciende el stack con demo.py prepare, corre la prueba de humo de la colección de Postman y la traza, y abre las pestañas. Usar cuando el usuario diga "inicia la demo", "prepara la demo", "levanta el sistema" o similar.
---

# Iniciar la demo EP3 (portátil)

Proyecto en `C:\Users\lizbe\Proyectos\ep3-sistema-recomendacion`, fuera de OneDrive: OneDrive rompe `.git`. Python del proyecto: `.venv\Scripts\python`. Antes de correr scripts, poner `$env:PYTHONIOENCODING='utf-8'`. Llevar un plan con `update_plan` y avisar al usuario en una línea al pasar de un paso al siguiente.

## 1. Credenciales (las pega el usuario)
1. Abrir el archivo: `Start-Process notepad "$env:USERPROFILE\.aws\credentials"`.
2. Darle al usuario estas instrucciones, en este orden:
   - Edge → AWS Academy → Learner Lab → **Start Lab** → esperar el círculo verde.
   - **AWS Details → AWS CLI: Show** → copiar el bloque → **reemplazar** todo el contenido del Bloc de notas → Ctrl+S.
   - Clic en **AWS** junto al círculo verde, para que la consola quede con la sesión iniciada en Edge (la usan las pestañas del paso 4). Dejar esa pestaña abierta.
3. **Nunca** pedir las credenciales en el chat. Esperar a que el usuario confirme.
4. Validar: `.venv\Scripts\python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"`. Si falla con un error de token expirado o inválido, pedirle que repita el paso 2 (no reintentar en bucle).

## 2. Encender el sistema (~10 min; ~18 min si hay que recrear el endpoint)
- Antes, ver si existe el endpoint: `.venv\Scripts\python infra\scripts\sagemaker_endpoint.py status`. Si no existe (se borra entre demos para ahorrar), recrearlo desde el artefacto que sigue en S3: `.venv\Scripts\python infra\scripts\sagemaker_endpoint.py deploy autoscale`, con `run_in_background`; espera a InService (~5–8 min). No usar `package`/`upload`: en el portátil no está `models/`.
- Luego: `.venv\Scripts\python infra\scripts\demo.py prepare`, con `run_in_background` (tarda más de 2 minutos). Al terminar, leer la salida.
- Lo esperado es "Listo para presentar.". Si alguna línea dice FALLA: esperar 2 min y correr `demo.py check`, hasta 3 veces. Si un 500 persiste, el NLB todavía no está listo (ver Parte D de `docs/EP3_Paso_a_paso_Demo.md`).
- `prepare` regenera `build/postman_environment.json`. Las API keys no cambian entre días, así que Postman no necesita reimportarlo salvo que se haya hecho `ecs_deploy.py down`.

## 3. Prueba de humo (~1–2 min)
- `.venv\Scripts\python infra\scripts\smoke_demo.py --full`. Corre los casos de la colección de Postman (403, CP-05 con ALS, ficha, cold-start, 422, KPIs, promociones, widget, reporte asíncrono hasta `done` y descarga, CP-12 → DLQ) y verifica en Logs Insights la traza rank → reco → business-api.
- Usa request ids `smoke-*`, así que la consulta guardada `reco-ep3/demo-traza` (que filtra `demo-home-001`) queda limpia.
- No corre CP-08 (429), para no gastar la cuota del plan partner.
- Si algo falla, informar qué falló y el plan B de la Parte D de la guía. No tocar la infraestructura sin preguntar.

## 4. Pestañas
- Confirmar que la consola de AWS ya está abierta en Edge (paso 1.2). Luego: `cmd /c docs\abrir_pestanas_demo.bat`.

## 5. Informe final al usuario
- Resultado de `prepare`, del smoke y de la traza.
- URLs: dashboard `http://reco-ep3-public-349259138.us-east-1.elb.amazonaws.com/` (en Edge: "Avanzado → Continuar a …(no seguro)"), Swagger `https://sa0tjw1dzi.execute-api.us-east-1.amazonaws.com/prod/docs`.
- Lo que queda manual: en Postman, elegir el entorno **"Recomendador EP3 (demo)"** y correr CP-05 una vez. En Logs Insights, cargar la consulta guardada `reco-ep3/demo-traza` con rango 15m, sin presionar Run. Zoom al 125%. **No molestar** en Windows. Dejar la pantalla en las láminas.
- Costo: encendido ≈ USD 0,36/h. Al terminar, usar `/apagar-demo`.
