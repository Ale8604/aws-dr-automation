import os
import logging
import time
import boto3
import botocore

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
	"""Create a Launch Template + Auto Scaling Group using the provided AMI.

	Expects `AmiId` and optional `InstanceCount`, `InstanceType`, `SecurityGroupIds`, `SubnetIds` (comma-separated or list), `KeyName`, `TargetGroupArn`.
	Returns: { "asgName": str, "launchTemplateId": str, "desiredCapacity": int }
	"""
	region = os.environ.get("AWS_REGION", "us-west-2")
	ec2 = boto3.client("ec2", region_name=region)
	autoscaling = boto3.client("autoscaling", region_name=region)

	ami = event.get("AmiId")
	if not ami:
		return {"error": "AmiId is required"}

	count = int(event.get("InstanceCount", 4))
	instance_type = event.get("InstanceType", "t3.medium")
	sg_ids = event.get("SecurityGroupIds")
	subnet_ids = event.get("SubnetIds") or event.get("SubnetId")
	key_name = event.get("KeyName")
	name_tag = event.get("Name", "dr-instance")

	# normalize subnet ids to CSV for ASG
	if isinstance(subnet_ids, list):
		vpc_zone = ",".join(subnet_ids)
	elif isinstance(subnet_ids, str):
		vpc_zone = subnet_ids
	else:
		vpc_zone = None

	lt_name = f"drp-lt-{int(time.time())}"
	asg_name = f"drp-asg-{int(time.time())}"

	lt_data = {
		"ImageId": ami,
		"InstanceType": instance_type,
	}
	if sg_ids:
		lt_data["SecurityGroupIds"] = sg_ids
	if key_name:
		lt_data["KeyName"] = key_name

	try:
		# create launch template
		resp = ec2.create_launch_template(LaunchTemplateName=lt_name, LaunchTemplateData=lt_data)
		lt_id = resp.get("LaunchTemplate", {}).get("LaunchTemplateId")
		logger.info("Created launch template %s", lt_id)

		# create ASG
		asg_kwargs = {
			"AutoScalingGroupName": asg_name,
			"LaunchTemplate": {"LaunchTemplateId": lt_id, "Version": "$Latest"},
			"MinSize": count,
			"MaxSize": count,
			"DesiredCapacity": count,
		}
		asg_kwargs["Tags"] = [
			{
				"Key": "Name",
				"Value": name_tag,
				"PropagateAtLaunch": True
			}
		]
		if vpc_zone:
			asg_kwargs["VPCZoneIdentifier"] = vpc_zone
		target_group_arn = event.get("TargetGroupArn")
		if target_group_arn:
			asg_kwargs["TargetGroupARNs"] = [target_group_arn]

		if not vpc_zone:
			raise Exception("SubnetIdsRequired")

		autoscaling.create_auto_scaling_group(**asg_kwargs)
		logger.info("Created ASG %s (desired=%s)", asg_name, count)

		# Wait for instances to enter service in the ASG and collect instance IDs
		instance_ids = []
		wait_seconds = int(os.environ.get("ASG_WAIT_SECONDS", 300))
		poll_interval = int(os.environ.get("ASG_POLL_INTERVAL", 10))
		max_iters = max(1, wait_seconds // poll_interval)
		for _ in range(max_iters):
			asg_desc = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
			groups = asg_desc.get("AutoScalingGroups", [])
			if groups:
				insts = groups[0].get("Instances", [])
				# consider only InService instances
				instance_ids = [i.get("InstanceId") for i in insts if i.get("LifecycleState") == "InService"]
				if len(instance_ids) >= count:
					break
			time.sleep(poll_interval)

		# If we found instance ids, optionally verify via EC2 describe
		if instance_ids:
			desc = ec2.describe_instances(InstanceIds=instance_ids)
			# flatten instance ids to ensure valid list
			flat_ids = []
			for r in desc.get("Reservations", []):
				for inst in r.get("Instances", []):
					flat_ids.append(inst.get("InstanceId"))
			instance_ids = flat_ids

		return {"asgName": asg_name, "launchTemplateId": lt_id, "desiredCapacity": count, "instanceIds": instance_ids}

	except botocore.exceptions.ClientError as e:
		logger.exception("Error creating launch template / asg")
		return {"error": str(e)}

	except Exception:
		logger.exception("Unexpected error deploying ASG")
		raise

