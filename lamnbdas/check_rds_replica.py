import os
import logging
import boto3
import botocore

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
	"""Validate that a given RDS Read Replica in us-west-2 is in status 'available'.

	Expects `ReplicaIdentifier` (or `DBInstanceIdentifier`) in the event.
	Returns JSON: { "replicaAvailable": bool, "replicaStatus": str, "dbInstanceIdentifier": str }
	"""
	region = os.environ.get("AWS_REGION", "us-west-2")
	rds = boto3.client("rds", region_name=region)

	replica_id = event.get("ReplicaIdentifier") or event.get("DBInstanceIdentifier")
	if not replica_id:
		logger.error("ReplicaIdentifier missing from event")
		return {"replicaAvailable": False, "error": "ReplicaIdentifier is required"}

	try:
		resp = rds.describe_db_instances(DBInstanceIdentifier=replica_id)
		db = resp.get("DBInstances", [])[0]
		status = db.get("DBInstanceStatus")
		lag = db.get("ReplicaLag")
		available = status == "available"
		logger.info("Replica %s status=%s lag=%s", replica_id, status, lag)
		if not available:
			# Signal Step Functions to retry by raising a specific error
			raise Exception("ReplicaNotReady")
		if lag is not None and lag > 30:
			logger.warning("Replica lag too high: %s seconds", lag)
			raise Exception("ReplicaLagTooHigh")
		return {"replicaAvailable": available, "replicaStatus": status, "dbInstanceIdentifier": replica_id}

	except botocore.exceptions.ClientError as e:
		code = e.response.get("Error", {}).get("Code")
		logger.exception("Error describing DB instance: %s", code)
		# If the instance is not found, treat as not available
		return {"replicaAvailable": False, "error": str(e)}

	except Exception as e:
		logger.exception("Unexpected error checking replica")
		raise
