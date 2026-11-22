# Pruebas de carga (CP-14) — 2026-11-22

Herramienta: Locust 2.47 (`tests/load/locustfile.py`) desde el PC del equipo (Chile) contra el ALB público
en us-east-1. Flujo probado: **widget → ALB → reco-service → ALB interno → rank-service → SageMaker `reco-als`**.
Mezcla de tráfico: 60% home con historial, 25% ficha de producto, 5% carrito, 10% anónimos (cold-start);
usuarios al azar entre 5.000 (la mayoría de las peticiones no sale de la caché).

Se reportan dos latencias:
- **Servidor** (CloudWatch, ALB `TargetResponseTime`): la que corresponde al SLA p95 < 200 ms.
- **Cliente** (Locust): suma ~130 ms de red Chile → Virginia, que no existe para clientes en la región.

Archivos: `<escenario>_stats.csv` / `.html` (Locust), `cw_<escenario>.json` (CloudWatch por minuto),
`<escenario>_counts.json` (respuestas en respaldo y desde caché). `iter1_025vcpu/` = primera iteración.

## Resultados

| Escenario | Config. reco/rank | Throughput | p95 servidor | p99 servidor | p95 cliente | Errores | Respaldo | SageMaker p95 |
|---|---|---|---|---|---|---|---|---|
| Normal (30 usuarios, 6 min) | 0,25 vCPU, mín. 1 | 25,5 req/s | **108 ms** ✅ | 257 ms | 210 ms | **0%** | 0,3% | 54 ms |
| Pico, iteración 1 (150 usuarios) | 0,25 vCPU, mín. 1 | 60 req/s | 3.440 ms ❌ | 5.648 ms | 3.400 ms | 0,05% | 4,8% | 58 ms |
| Estrés (+50 usuarios/min hasta 400) | 0,25 vCPU, mín. 1 (escaló a 3) | hasta 141 req/s | 4.468 ms ❌ | 5.034 ms | 3.800 ms | 0,04% | 60% | 180 ms |
| **Pico, iteración 2** (150 usuarios) | **0,5 vCPU, mín. 2** | **100 req/s** | **174 ms** ✅ | 290 ms | 300 ms | **0%** | 86,6% ⚠️ | 3.207 ms |

## Hallazgos y optimizaciones

1. **Respuestas vacías (corregido).** En la primera corrida normal, el 1,4% de las respuestas llegó sin
   productos: todos los candidatos del modelo estaban sin stock y la regla estricta los descartaba.
   reco-service ahora completa la lista con populares con stock (`backfilled`). Segunda corrida: 0 errores.
2. **Cuello de botella 1: CPU de reco-service.** Con 0,25 vCPU y una tarea, reco-service llegó al 100% de CPU
   con 60 req/s (≈ 45 req/s por tarea) y el auto-scaling por CPU (> 60%, 3 min de alarma + arranque de tarea)
   no alcanzó a reaccionar en un pico de 3 min. Se alineó al diseño de la EP2 (0,5 vCPU, mínimo 2 réplicas):
   el mismo pico pasó a 100 req/s con p95 de servidor 174 ms y 0 errores.
3. **Cuello de botella 2: el endpoint de SageMaker.** Resuelto el anterior, la única instancia `ml.m5.large`
   se saturó (~120 invocaciones/s, ModelLatency p95 3,2 s). reco-service cortó a los 150 ms y respondió con el
   respaldo (86,6%): **el SLA de latencia y la disponibilidad se mantuvieron, pero se perdió personalización**.
   El auto-scaling del endpoint (objetivo 600 invocaciones/instancia/min) recién pidió una 2.ª instancia
   después del pico.
4. **El sistema nunca se cayó.** En el estrés (hasta 141 req/s, 4× la carga normal) la tasa de errores fue
   0,04%: el Fallback Handler cumplió su función de degradación elegante.
5. **Alarmas.** `reco-latency-p95-warning` y `-critical` pasaron a ALARM durante el pico y el estrés y
   volvieron a OK al terminar. `model-latency-p95` no se activó porque exige 5 minutos seguidos y la
   saturación duró ~3 min → bajar a 3 de 5 puntos.

## Capacidad estimada para el pico del caso (670 req/s, CyberDay)

Por tarea: reco-service ≈ 45 req/s con 0,25 vCPU y ≈ 90 req/s con 0,5 vCPU (CPU 57% a 50 req/s por tarea);
rank-service ≈ 80 req/s con 0,5 vCPU. Con caché de ~35% de aciertos llegan ~435 req/s a los servicios:

| Componente | Necesario para 670 req/s | Configuración actual |
|---|---|---|
| reco-service (0,5 vCPU) | ~8 tareas (con margen del 40%) | 2–6 |
| rank-service (0,5 vCPU) | ~6 tareas | 2–4 |
| SageMaker | ~4 `ml.m5.large` o 2 `ml.c5.xlarge` con inferencia en tiempo real por cada petición | 1–2 |

**Optimización recomendada (siguiente iteración):** precalcular el top-200 por usuario en batch y servirlo
desde DynamoDB/caché (como define el diseño EP1/EP2), dejando el endpoint de SageMaker solo para
cold-start y ficha de producto. Así la carga sobre el modelo deja de crecer con el tráfico. Otras: subir la
caché (TTL 15 min ya da 34% de aciertos con usuarios aleatorios; en tráfico real será mayor), escalar por
`RequestCountPerTarget` en vez de CPU (reacciona antes) y mínimo de 2 instancias del endpoint en campañas.
