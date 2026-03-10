import os
import logging
import boto3
import botocore

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
	"""Verify that provided EC2 instance IDs are healthy in the given ALB Target Group.

	Expects `InstanceIds` (list) and `TargetGroupArn` in the event.
	Returns: { "allHealthy": bool, "healthy": [...], "unhealthy": [...], "details": {...} }
	"""
	region = os.environ.get("AWS_REGION", "us-west-2")
	elbv2 = boto3.client("elbv2", region_name=region)

	instance_ids = event.get("InstanceIds") or []
	if not instance_ids:
		raise Exception("NoInstanceIdsProvided")
	target_group_arn = event.get("TargetGroupArn")
	if not target_group_arn:
		return {"allHealthy": False, "error": "TargetGroupArn is required"}

	try:
		resp = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
		thd = resp.get("TargetHealthDescriptions", [])
		state_by_id = {d.get("Target", {}).get("Id"): d.get("TargetHealth", {}).get("State") for d in thd}

		healthy = []
		unhealthy = []
		details = {}
		for iid in instance_ids:
			state = state_by_id.get(iid)
			details[iid] = state
			if state == "healthy":
				healthy.append(iid)
			else:
				unhealthy.append(iid)

		all_healthy = len(unhealthy) == 0 and len(instance_ids) > 0
		logger.info("Target health for %s: healthy=%s unhealthy=%s", instance_ids, healthy, unhealthy)
		if not all_healthy:
			# Let Step Functions retry the healthcheck by raising a specific error
			raise Exception("UnhealthyTargets")
		return {"allHealthy": all_healthy, "healthy": healthy, "unhealthy": unhealthy, "details": details}

	except botocore.exceptions.ClientError as e:
		logger.exception("Error checking target group health")
		return {"allHealthy": False, "error": str(e)}

	except Exception:
		logger.exception("Unexpected error verifying target group health")
		raise

