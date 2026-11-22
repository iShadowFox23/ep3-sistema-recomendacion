"""Dashboard de negocio: consume business-api (KPIs, promociones, explicación de recomendaciones)."""
import os

import streamlit as st

BUSINESS_API_URL = os.getenv("BUSINESS_API_URL", "http://localhost:8082")

st.set_page_config(page_title="Recomendaciones - Equipo 6", layout="wide")
st.title("Sistema de recomendación - dashboard de negocio")
st.caption(f"Fuente: {BUSINESS_API_URL}")
st.info("En construcción: KPIs, promociones sugeridas e interpretabilidad.")
