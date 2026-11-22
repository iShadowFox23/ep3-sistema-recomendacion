"""Dashboard de negocio del sistema de recomendación (Streamlit).

Consume únicamente business-api (no accede a los datos directamente), igual que lo haría cualquier
cliente empresarial. Secciones:
1. KPIs del negocio      embudo vista → carrito → compra, conversión y visitantes nuevos (cold-start)
2. Recomendaciones       predicción en vivo con el motivo de cada producto y las reglas aplicadas
3. Promociones           acciones comerciales sugeridas (prescriptivo)
4. Reportes              reportes batch asíncronos (RabbitMQ → report-worker)
5. Calidad del modelo    métricas offline del modelo ALS frente al baseline de popularidad

Variables de entorno: BUSINESS_API_URL, BUSINESS_API_KEY (API Gateway), METRICS_PATH.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from api import ApiError, BusinessApi, parse_items

API_URL = os.getenv("BUSINESS_API_URL", "http://localhost:8082")
METRICS_PATH = os.getenv("METRICS_PATH", "reports/metrics_test.json")

# Paleta categórica de referencia (validada para daltonismo): slot 1 azul, slot 2 naranjo.
SERIES_1, SERIES_2 = "#2a78d6", "#eb6834"
STRATEGY_TEXT = {
    "als": "Personalizada (filtrado colaborativo ALS sobre su historial)",
    "item_similarity": "Productos parecidos al que está viendo o tiene en el carrito",
    "popular_category": "Cold-start: populares de su categoría (sin historial suficiente)",
    "popular": "Cold-start: populares generales (sin historial)",
    "fallback_popular": "Respaldo: el modelo no respondió a tiempo, se muestran populares con stock",
    "fallback_popular_category": "Respaldo: el modelo no respondió a tiempo, populares de la categoría",
}
RULE_TEXT = {
    "removed_duplicates": "Duplicados",
    "removed_in_context": "Ya en pantalla o carrito",
    "removed_unavailable": "Sin stock",
    "removed_diversity": "Exceso de una categoría",
    "refilled_diversity": "Repuestos para completar",
}
ACTION_TEXT = {"destacar": "⭐ Destacar", "promocionar_descuento": "🏷️ Promocionar con descuento"}

st.set_page_config(page_title="Recomendaciones - Equipo 6", page_icon="🛒", layout="wide")


@st.cache_resource
def get_api() -> BusinessApi:
    return BusinessApi(API_URL, os.getenv("BUSINESS_API_KEY") or None)


api = get_api()


@st.cache_data(ttl=600)
def load_metrics(path: str) -> dict | None:
    """Métricas offline del modelo: archivo local (Docker) o s3://bucket/clave (ECS)."""
    try:
        if path.startswith("s3://"):
            import boto3

            bucket, _, key = path[len("s3://") :].partition("/")
            return json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def show_error(exc: ApiError) -> None:
    st.error(f"business-api respondió {exc.status or 'sin conexión'}: {exc.message} (`{exc.code}`)")


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def line_chart(df: pd.DataFrame, y: str, title: str, fmt: str, color: str) -> alt.Chart:
    """Una serie por gráfico (nunca doble eje), línea de 2 px, tooltip con crosshair."""
    base = alt.Chart(df).encode(x=alt.X("date:T", title=None, axis=alt.Axis(format="%d-%b", grid=False)))
    line = base.mark_line(strokeWidth=2, color=color).encode(y=alt.Y(f"{y}:Q", title=None, axis=alt.Axis(format=fmt)))
    hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover", empty=False)
    points = base.mark_point(size=80, filled=True, color=color).encode(
        y=f"{y}:Q",
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Fecha", format="%Y-%m-%d"), alt.Tooltip(f"{y}:Q", title=title, format=fmt)],
    ).add_params(hover)
    rule = base.mark_rule(color="gray", strokeWidth=1).encode(opacity=alt.condition(hover, alt.value(0.4), alt.value(0)))
    return (line + rule + points).properties(title=title, height=260)


# ============================================================================= encabezado

st.title("🛒 Sistema de recomendación — panel de negocio")
st.caption(f"Retail de tecnología (caso EP3, dataset RetailRocket como sustituto de Shopify) · fuente: business-api `{API_URL}`")

SECTIONS = ["📈 KPIs del negocio", "🎯 Recomendaciones en vivo", "🏷️ Promociones sugeridas", "📄 Reportes", "🧪 Calidad del modelo"]
# Menú lateral en vez de pestañas: conserva la sección al enviar formularios y solo consulta la API de la sección visible.
section = st.sidebar.radio("Sección", SECTIONS, key="section")
st.sidebar.caption("Equipo 6 · ITY1102 · EP3")

# ============================================================================= 1. KPIs

if section == SECTIONS[0]:
    try:
        default = api.kpis()
    except ApiError as exc:
        show_error(exc)
        default = None
    if default:
        last = date.fromisoformat(default["to"])
        col_a, col_b, _ = st.columns([1, 1, 2])
        start = col_a.date_input("Desde", date.fromisoformat(default["from"]), max_value=last, key="kpi_from")
        end = col_b.date_input("Hasta", last, max_value=last, key="kpi_to")
        try:
            data = default if (str(start), str(end)) == (default["from"], default["to"]) else api.kpis(str(start), str(end))
        except ApiError as exc:
            show_error(exc)
            data = None
        if data:
            s = data["summary"]
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Visitantes", f"{s['visitors']:,}".replace(",", "."))
            c2.metric("Conversión", pct(s["conversion_rate"]), help="Compradores / visitantes")
            c3.metric("Vista → carrito", pct(s["view_to_cart_rate"]), help="Carritos / vistas")
            c4.metric("Carrito → compra", pct(s["cart_to_purchase_rate"]), help="Compras / carritos")
            c5.metric("Visitantes nuevos", pct(s["new_visitor_share"]), help="Sin historial previo: casos cold-start")
            st.caption(
                f"Periodo {data['from']} → {data['to']} ({s['days']} días). Meta del caso: subir la conversión "
                "de 1,5% a 3,5%. El alto % de visitantes nuevos explica por qué el sistema necesita estrategias de cold-start. "
                "El último día del dataset (2015-09-18) está incompleto."
            )
            daily = pd.DataFrame(data["daily"])
            daily["date"] = pd.to_datetime(daily["date"])
            left, right = st.columns(2)
            left.altair_chart(line_chart(daily, "conversion_rate", "Conversión diaria", ".2%", SERIES_1), use_container_width=True)
            right.altair_chart(line_chart(daily, "visitors", "Visitantes diarios", ",d", SERIES_1), use_container_width=True)
            with st.expander("Ver tabla diaria"):
                st.dataframe(daily, hide_index=True, use_container_width=True)

# ============================================================================= 2. Recomendaciones

if section == SECTIONS[1]:
    st.markdown("Prueba el modelo desplegado en SageMaker tal como lo vería el cliente en la tienda, con el **motivo** de cada recomendación.")
    with st.form("predict"):
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        user_text = c1.text_input("ID de visitante", "257597", help="Vacío = visitante anónimo")
        context = c2.selectbox("Ubicación del widget", ["home", "pdp", "cart"], format_func={"home": "Home", "pdp": "Ficha de producto", "cart": "Carrito"}.get)
        items_text = c3.text_input("Producto(s) en pantalla / carrito", "", help="IDs separados por coma (para ficha o carrito)")
        k = c4.slider("Cantidad", 1, 20, 6)
        submitted = st.form_submit_button("Recomendar", type="primary")
    if submitted:
        user_id = int(user_text) if user_text.strip().isdigit() else None
        try:
            out = api.predict(user_id, context, k, parse_items(items_text), None)
        except ApiError as exc:
            show_error(exc)
            out = None
        if out:
            strategy = out.get("strategy", "")
            c1, c2, c3 = st.columns(3)
            c1.metric("Estrategia", strategy)
            c2.metric("Cold-start", "Sí" if out.get("cold_start") else "No")
            c3.metric("Modo", "Respaldo" if out.get("degraded") else "Normal")
            st.info(STRATEGY_TEXT.get(strategy, strategy))
            rows = []
            for pos, item in enumerate(out.get("items", []), start=1):
                reason = item.get("reason") or {}
                if reason.get("type") == "similar_to_history":
                    why = f"Parecido al producto {reason.get('item_id')} que vio (similitud {reason.get('similarity', 0):.0%})"
                else:
                    why = STRATEGY_TEXT.get(reason.get("type", ""), reason.get("type", ""))
                rows.append(
                    {
                        "#": pos,
                        "Producto": str(item["item_id"]),  # IDs como texto: sin separador de miles
                        "Categoría": "" if item.get("category_id") is None else str(item["category_id"]),
                        "Stock": {True: "Sí", False: "No", None: "Sin dato"}.get(item.get("available")),
                        "Puntaje": item.get("score"),
                        "¿Por qué se recomienda?": why,
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            rules = out.get("rules") or {}
            if rules:
                st.subheader("Reglas de negocio aplicadas sobre los candidatos del modelo")
                rule_df = pd.DataFrame([{"Regla": RULE_TEXT.get(k_, k_), "Productos": v} for k_, v in rules.items()])
                bars = alt.Chart(rule_df).mark_bar(color=SERIES_1, cornerRadiusEnd=4, height=18).encode(
                    x=alt.X("Productos:Q", title="Productos descartados o repuestos", axis=alt.Axis(tickMinStep=1)),
                    y=alt.Y("Regla:N", title=None, sort=None, axis=alt.Axis(labelLimit=220)),
                    tooltip=["Regla", "Productos"],
                )
                labels = bars.mark_text(align="left", dx=4, color="gray").encode(text="Productos:Q")
                st.altair_chart((bars + labels).properties(height=40 * len(rule_df)), use_container_width=True)
            st.caption(f"request-id `{out.get('request_id')}` · caché: {out.get('cache')}")

# ============================================================================= 3. Promociones

if section == SECTIONS[2]:
    st.markdown(
        "Acciones comerciales sugeridas por el sistema a partir de la demanda observada y el stock. "
        "**Destacar**: convierte más del doble que la referencia. **Descuento**: mucho interés y carritos, pero pocas compras."
    )
    c1, c2, _ = st.columns([1, 1, 2])
    cat_text = c1.text_input("Categoría (opcional)", "")
    limit = c2.slider("Máximo de productos", 5, 50, 15)
    try:
        promo = api.promotions(int(cat_text) if cat_text.strip().isdigit() else None, limit)
    except ApiError as exc:
        show_error(exc)
        promo = None
    if promo:
        st.caption(f"Periodo analizado: {promo['from']} → {promo['to']}")
        if not promo["items"]:
            st.warning("No hay sugerencias para este filtro.")
        else:
            df = pd.DataFrame(promo["items"])
            df["Acción"] = df["action"].map(ACTION_TEXT)
            df["Conversión"] = df["conversion_rate"].map(pct)
            df["item_id"] = df["item_id"].astype(str)
            df["category_id"] = df["category_id"].map(lambda c: "" if pd.isna(c) else str(int(c)))
            view = df.rename(columns={"item_id": "Producto", "category_id": "Categoría", "views": "Vistas", "carts": "Carritos", "transactions": "Compras", "reason": "Motivo"})
            cols = ["Acción", "Producto", "Categoría", "Vistas", "Carritos", "Compras", "Conversión", "Motivo"]
            st.dataframe(view[cols], hide_index=True, use_container_width=True)
            st.download_button("Descargar CSV", view[cols].to_csv(index=False).encode("utf-8"), "promociones.csv", "text/csv")

# ============================================================================= 4. Reportes

if section == SECTIONS[3]:
    st.markdown("Los reportes se generan en segundo plano: la API responde al instante y **report-worker** los procesa desde RabbitMQ.")
    with st.form("report"):
        c1, c2, c3 = st.columns(3)
        report_type = c1.selectbox(
            "Tipo",
            ["kpi_summary", "top_products", "cold_start_coverage"],
            format_func={"kpi_summary": "Resumen de KPIs diarios", "top_products": "Productos top", "cold_start_coverage": "Cobertura cold-start"}.get,
        )
        r_from = c2.date_input("Desde", date(2015, 9, 5))
        r_to = c3.date_input("Hasta", date(2015, 9, 18))
        create = st.form_submit_button("Solicitar reporte", type="primary")
    if create:
        try:
            job = api.create_report(report_type, {"from": str(r_from), "to": str(r_to)})
            st.session_state.setdefault("jobs", []).insert(0, job["job_id"])
            st.success(f"Reporte encolado: `{job['job_id']}` (estado: {job['status']})")
        except ApiError as exc:
            show_error(exc)
    jobs = st.session_state.get("jobs", [])
    if jobs:
        st.button("Actualizar estados")
        for job_id in jobs[:10]:
            try:
                job = api.report(job_id)
            except ApiError as exc:
                show_error(exc)
                continue
            icon = {"queued": "⏳", "running": "⚙️", "retrying": "🔁", "done": "✅", "failed": "❌"}.get(job["status"], "")
            c1, c2 = st.columns([3, 1])
            c1.write(f"{icon} **{job['report_type']}** · `{job_id[:8]}` · {job['status']}" + (f" · {job['rows']} filas" if job.get("rows") is not None else "") + (f" · {job['error']}" if job.get("error") else ""))
            if job["status"] == "done" and job.get("download_url"):
                try:
                    c2.download_button("Descargar", api.download(job["download_url"]), f"{job['report_type']}_{job_id[:8]}.csv", "text/csv", key=job_id)
                except Exception:
                    c2.write("Descarga no disponible")

# ============================================================================= 5. Calidad del modelo

if section == SECTIONS[4]:
    m = load_metrics(METRICS_PATH)
    if m is None:
        st.warning(f"No se encontró {METRICS_PATH} (generar con ml/evaluate.py).")
    else:
        als, base, k = m["als"], m["popularity_baseline"], m["k"]
        st.markdown(
            f"Evaluación offline en el periodo de **test** (últimas 2 semanas, {als['users_evaluated']:,} usuarios). "
            f"Se mide si el modelo anticipa productos **nuevos** para el usuario, comparado con recomendar lo más popular."
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Precision@{k}", f"{als[f'precision@{k}']:.4f}", f"×{m['lift_vs_baseline'][f'precision@{k}']:.1f}", help="Mejora frente a recomendar lo más popular")
        c2.metric(f"Recall@{k}", f"{als[f'recall@{k}']:.4f}", f"×{m['lift_vs_baseline'][f'recall@{k}']:.1f}", help="Mejora frente a recomendar lo más popular")
        c3.metric(f"NDCG@{k}", f"{als[f'ndcg@{k}']:.4f}", f"×{m['lift_vs_baseline'][f'ndcg@{k}']:.1f}", help="Mejora frente a recomendar lo más popular")
        c4.metric("Cobertura de catálogo", pct(als["catalog_coverage"]), f"{pct(base['catalog_coverage'])} base", delta_color="off", help="% del catálogo que aparece en alguna recomendación (ALS vs popularidad)")
        names = [f"precision@{k}", f"recall@{k}", f"f1@{k}", f"ndcg@{k}"]
        df = pd.DataFrame(
            [{"Métrica": n, "Modelo": "ALS (desplegado)", "Valor": als[n]} for n in names]
            + [{"Métrica": n, "Modelo": "Popularidad (baseline)", "Valor": base[n]} for n in names]
        )
        chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("Métrica:N", title=None, sort=names, axis=alt.Axis(labelAngle=0)),
            xOffset=alt.XOffset("Modelo:N", sort=["ALS (desplegado)", "Popularidad (baseline)"]),
            y=alt.Y("Valor:Q", title=None, axis=alt.Axis(format=".3f")),
            color=alt.Color(
                "Modelo:N",
                scale=alt.Scale(domain=["ALS (desplegado)", "Popularidad (baseline)"], range=[SERIES_1, SERIES_2]),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=["Métrica", "Modelo", alt.Tooltip("Valor:Q", format=".4f")],
        )
        st.altair_chart(chart.properties(height=300), use_container_width=True)
        st.caption(
            f"Accuracy@{k} = {als[f'accuracy@{k}']:.5f} en ambos modelos: casi todos los pares usuario-producto son negativos, "
            "por eso accuracy no sirve para comparar recomendadores y se usan precision/recall/NDCG@k."
        )
        with st.expander("Detalle del modelo"):
            st.json(m.get("model", {}))
