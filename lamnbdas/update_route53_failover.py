import os
import logging
import boto3
import botocore

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _discover_lb_info(elbv2, dns_name=None, lb_arn=None):
	# Returns (dns_name, canonical_hosted_zone_id)
	if lb_arn:
		resp = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])
		lbs = resp.get("LoadBalancers", [])
		if not lbs:
			return None, None
		lb = lbs[0]
		return lb.get("DNSName"), lb.get("CanonicalHostedZoneId")

	if dns_name:
		# fetch all and match DNS name
		resp = elbv2.describe_load_balancers()
		for lb in resp.get("LoadBalancers", []):
			if lb.get("DNSName") == dns_name:
				return lb.get("DNSName"), lb.get("CanonicalHostedZoneId")
	return None, None


def lambda_handler(event, context):
	"""Update Route53 to point a record to the provided ALB in us-west-2.

	Expects: HostedZoneId (Route53), RecordName, and either TargetLoadBalancerDnsName OR LoadBalancerArn.
	Returns change info for the Route53 change.
	"""
	region = os.environ.get("AWS_REGION", "us-west-2")
	route53 = boto3.client("route53")
	elbv2 = boto3.client("elbv2", region_name=region)

	hosted_zone_id = event.get("HostedZoneId")
	record_name = event.get("RecordName")
	lb_dns = event.get("TargetLoadBalancerDnsName")
	lb_arn = event.get("LoadBalancerArn")

	if not hosted_zone_id or not record_name:
		return {"success": False, "error": "HostedZoneId and RecordName are required"}

	try:
		dns, lb_zone_id = _discover_lb_info(elbv2, dns_name=lb_dns, lb_arn=lb_arn)
		if not dns or not lb_zone_id:
			return {"success": False, "error": "Could not discover load balancer info"}

		change = {
			"Action": "UPSERT",
			"ResourceRecordSet": {
				"Name": record_name,
				"Type": "A",
				"AliasTarget": {
					"HostedZoneId": lb_zone_id,
					"DNSName": dns,
					"EvaluateTargetHealth": False,
				},
			},
		}

		resp = route53.change_resource_record_sets(HostedZoneId=hosted_zone_id, ChangeBatch={"Changes": [change]})
		logger.info("Route53 change submitted: %s", resp.get("ChangeInfo"))
		change_id = resp["ChangeInfo"]["Id"]
		waiter = route53.get_waiter("resource_record_sets_changed")
		waiter.wait(Id=change_id)
		return {"success": True, "changeInfo": resp.get("ChangeInfo"), "recordName": record_name}

	except botocore.exceptions.ClientError as e:
		logger.exception("Error updating Route53 record")
		return {"success": False, "error": str(e)}

	except Exception:
		logger.exception("Unexpected error updating Route53")
		raise

