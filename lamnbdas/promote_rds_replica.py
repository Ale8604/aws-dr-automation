#!/usr/bin/env python3
"""Lambda para promocionar una réplica RDS a instancia primaria (maestra).

Entrada esperada (evento):
  {
    "DBInstanceIdentifier": "my-replica-id",
    "Region": "us-west-2"  # opcional, por defecto us-west-2
  }

Salida: JSON con estado y mensaje / details.
"""
import os
import logging
import time
import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def lambda_handler(event, context):
    identifier = None
    mode = None
    if isinstance(event, dict):
        identifier = event.get("DBInstanceIdentifier") or event.get("db_instance_identifier")
        mode = event.get("mode")

    if not identifier:
        return {"status": "error", "message": "Missing DBInstanceIdentifier in event"}

    region = event.get("Region") or event.get("region") or "us-west-2"
    rds = boto3.client("rds", region_name=region)

    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=identifier)
        instances = resp.get("DBInstances", [])
        if not instances:
            return {"status": "error", "message": f"DB instance {identifier} not found in {region}"}
        db = instances[0]

        # If mode==create_replica, create a read replica and return
        if mode == "create_replica":
            source = event.get("SourceDBInstanceIdentifier") or identifier
            new_id = event.get("NewDBInstanceIdentifier") or f"{source}-replica-{int(time.time())}"
            LOG.info("Creating read replica %s from source %s in %s", new_id, source, region)
            try:
                resp_create = rds.create_db_instance_read_replica(DBInstanceIdentifier=new_id, SourceDBInstanceIdentifier=source)
                waiter = rds.get_waiter("db_instance_available")
                try:
                    waiter.wait(DBInstanceIdentifier=new_id, WaiterConfig={"Delay": 15, "MaxAttempts": 40})
                except ClientError as we:
                    LOG.warning("Waiter failed: %s", we)
                final_create = rds.describe_db_instances(DBInstanceIdentifier=new_id)["DBInstances"][0]
                return {"status": "success", "message": f"Read replica {new_id} created", "instance": final_create}
            except ClientError as e:
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
                LOG.error("RDS ClientError %s: %s", code, msg)
                return {"status": "error", "message": msg, "code": code}

        # Detectar si ya es instancia primaria (no read replica)
        if not db.get("ReadReplicaSourceDBInstanceIdentifier"):
            return {"status": "skipped", "message": f"{identifier} is not a read replica (already primary?)"}

        status = db.get("DBInstanceStatus", "unknown")
        if status.lower() in ("promoting", "modifying"):
            return {"status": "in_progress", "message": f"{identifier} promotion already in progress (status={status})"}

        # Ejecutar promoción
        LOG.info("Promoting read replica %s in %s", identifier, region)
        promo = rds.promote_read_replica(DBInstanceIdentifier=identifier)

        # Esperar a que la instancia esté disponible
        waiter = rds.get_waiter("db_instance_available")
        try:
            waiter.wait(DBInstanceIdentifier=identifier, WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        except ClientError as we:
            LOG.warning("Waiter failed: %s", we)

        # Reconsultar estado final
        final = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
        return {"status": "success", "message": f"Promotion initiated for {identifier}", "instance": final}

    except ClientError as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
        LOG.error("RDS ClientError %s: %s", code, msg)
        if code in ("DBInstanceNotFound", "InvalidDBInstanceIdentifier.Fault"):
            return {"status": "error", "message": f"DB instance {identifier} not found: {msg}"}
        if code == "InvalidDBInstanceState" and "promoting" in msg.lower():
            return {"status": "in_progress", "message": f"Promotion already in progress: {msg}"}
        return {"status": "error", "message": msg, "code": code}


if __name__ == "__main__":
    # sencillo test local
    print(lambda_handler({"DBInstanceIdentifier": "my-replica", "Region": "us-west-2"}, None))
