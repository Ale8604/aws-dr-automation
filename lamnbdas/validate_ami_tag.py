import os
import logging
import boto3
import botocore

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
	"""Find the most recent AMI in us-west-2 with tag DR_READY=true.

	Returns: { "amiExists": bool, "amiId": str (if exists), "creationDate": str }
	"""
	region = os.environ.get("AWS_REGION", "us-west-2")
	ec2 = boto3.client("ec2", region_name=region)

	owner = event.get("Owner")
	filters = [
		{"Name": "tag:DR_READY", "Values": ["true"]},
		{"Name": "state", "Values": ["available"]}
	]

	try:
		if owner:
			resp = ec2.describe_images(Owners=[owner], Filters=filters)
		else:
			resp = ec2.describe_images(Filters=filters)

		images = resp.get("Images", [])
		if not images:
			logger.info("No AMIs found with DR_READY=true in %s", region)
			# Stop the workflow by raising an error that Step Functions can catch
			raise Exception("AMI_NOT_FOUND")

		# Sort by CreationDate desc and return the latest
		images.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
		ami = images[0]
		ami_id = ami.get("ImageId")
		creation = ami.get("CreationDate")
		logger.info("Found AMI %s created %s", ami_id, creation)
		return {"amiExists": True, "amiId": ami_id, "creationDate": creation}

	except botocore.exceptions.ClientError as e:
		logger.exception("Error searching for AMIs")
		return {"amiExists": False, "error": str(e)}

	except Exception:
		logger.exception("Unexpected error validating AMI tag")
		raise

