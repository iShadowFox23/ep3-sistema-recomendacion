"""Pruebas de carga (CP-14): escenarios normal, pico y estrés contra business-api."""
from locust import HttpUser, between, task


class BusinessApiUser(HttpUser):
    wait_time = between(0.5, 1.5)

    @task
    def health(self):
        self.client.get("/health")
