"""Data lake mínimo en S3 + catálogo de Glue consultable con Athena.

Sube los eventos crudos de RetailRocket y el catálogo de productos al bucket del proyecto y crea
la base `reco_ep3` con las tablas externas `events` y `catalog`. business-api (backend `athena`)
y report-worker consultan estas tablas.

    s3://reco-ep3-<cuenta>-<region>/datalake/events/events.csv
    s3://reco-ep3-<cuenta>-<region>/datalake/catalog/catalog.csv
    s3://reco-ep3-<cuenta>-<region>/athena-results/                (resultados de consultas)

Uso:
    python infra/scripts/datalake.py setup     # sube datos, crea base y tablas, prueba una consulta
    python infra/scripts/datalake.py query "SELECT count(*) FROM events"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "reco_ep3"


def context():
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"reco-ep3-{account}-{session.region_name}"
    return session, bucket


def run_query(athena, sql: str, bucket: str, database: str | None = DATABASE) -> list[list[str]]:
    kwargs = {"QueryExecutionContext": {"Database": database}} if database else {}
    qid = athena.start_query_execution(
        QueryString=sql, ResultConfiguration={"OutputLocation": f"s3://{bucket}/athena-results/"}, **kwargs
    )["QueryExecutionId"]
    while True:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if state["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    if state["State"] != "SUCCEEDED":
        raise RuntimeError(f"Athena {state['State']}: {state.get('StateChangeReason')}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return [[col.get("VarCharValue", "") for col in row["Data"]] for row in rows]


def setup() -> None:
    session, bucket = context()
    s3, athena = session.client("s3"), session.client("athena")
    uploads = {
        ROOT / "data/raw/retailrocket/events.csv": "datalake/events/events.csv",
        ROOT / "build/serving/catalog.csv": "datalake/catalog/catalog.csv",
    }
    for local, key in uploads.items():
        if not local.exists():
            sys.exit(f"falta {local.relative_to(ROOT)} (ver README)")
        s3.upload_file(str(local), bucket, key)
        print(f"subido s3://{bucket}/{key} ({local.stat().st_size / 1e6:.0f} MB)")

    run_query(athena, f"CREATE DATABASE IF NOT EXISTS {DATABASE}", bucket, database=None)
    run_query(athena, "DROP TABLE IF EXISTS events", bucket)
    run_query(
        athena,
        f"""CREATE EXTERNAL TABLE events (
              `timestamp` bigint, visitorid bigint, event string, itemid bigint, transactionid string)
            ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
            LOCATION 's3://{bucket}/datalake/events/'
            TBLPROPERTIES ('skip.header.line.count'='1')""",
        bucket,
    )
    run_query(athena, "DROP TABLE IF EXISTS catalog", bucket)
    run_query(
        athena,
        f"""CREATE EXTERNAL TABLE catalog (
              itemid bigint, category_id int, root_category_id int, available int)
            ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
            LOCATION 's3://{bucket}/datalake/catalog/'
            TBLPROPERTIES ('skip.header.line.count'='1')""",
        bucket,
    )
    print(f"base {DATABASE} con tablas events y catalog creada")
    rows = run_query(athena, "SELECT event, count(*) FROM events GROUP BY event ORDER BY 2 DESC", bucket)
    for row in rows:
        print("  ", " | ".join(row))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "query"])
    parser.add_argument("sql", nargs="?")
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    else:
        session, bucket = context()
        for row in run_query(session.client("athena"), args.sql, bucket):
            print(" | ".join(row))


if __name__ == "__main__":
    main()
