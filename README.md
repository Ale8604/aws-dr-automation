# DRP Automation — Disaster Recovery Failover

## 1. Project Overview

This repository contains an AWS Disaster Recovery (DR) automation implementation that orchestrates failover from the Primary region (us-east-1) to a DR region (us-west-2). The system is implemented with AWS Step Functions, AWS Lambda, and Terraform. A manual Systems Manager Automation Runbook (the "Red Button") triggers the DR workflow.

Target audience: DevOps engineers and SREs responsible for running and validating DR failovers.


## 2. Architecture Overview

This section describes the control plane and data plane components and how they map to the primary and DR regions.

**Primary Region (us-east-1)**

- **ALB (Application Load Balancer)**: Fronts the application and receives client traffic.
- **EC2 application instances**: Running in an Auto Scaling Group (ASG) behind the ALB.
- **RDS PostgreSQL (primary)**: The authoritative application database.

**DR Region (us-west-2)**

- **RDS Read Replica**: Replica of the primary database kept in sync and validated before failover.
- **Auto Scaling Group (DR)**: Initially desired capacity = 0. The failover process scales or launches instances (workflow uses 4 instances).
- **ALB (DR)**: Application Load Balancer in the DR region with an associated Target Group for DR instances.

**Orchestration & Runbooks**

- **Step Functions (`drp-workflow`)**: State machine that orchestrates validations, promotions, deployments, health checks, and DNS changes during failover.
- **Lambda functions**: Small task Lambdas perform region-specific operations (RDS checks/promote, AMI validation, EC2/ASG creation, ALB target registration, Route53 updates).
- **SSM Automation runbook (`DRP-Runbook-RedButton`)**: Manual trigger that starts the Step Functions execution and provides a controlled operator entry point.

Mermaid diagram (architecture):

```mermaid
flowchart LR
  subgraph Primary Region (us-east-1)
    direction TB
    P_ALB[ALB (Primary)]
    P_ASG[Auto Scaling Group (Primary)]
    P_RDS[RDS PostgreSQL (Primary)]
    P_ASG --> P_ALB
    P_RDS --- P_ASG
  end

  subgraph DR Region (us-west-2)
    direction TB
    DR_RDS[RDS Read Replica (DR)]
    DR_ASG[Auto Scaling Group (DR)]
    DR_ALB[ALB (DR)]
    DR_ASG --> DR_ALB
    DR_RDS --- DR_ALB
  end

  ControlPlane[Control Plane]
  ControlPlane --> SSM[SSM Automation: DRP-Runbook-RedButton]
  ControlPlane --> SF[Step Functions: drp-workflow]
  SF --> L1[Lambda: check_rds_replica]
  SF --> L2[Lambda: validate_ami_tag]
  SF --> L3[Lambda: promote_rds_replica]
  SF --> L4[Lambda: deploy_ec2_instances]
  SF --> L5[Lambda: check_targetgroup_health]
  SF --> L6[Lambda: update_route53_failover]

  SF --> Route53[Route53]
  Route53 --> Clients[Clients -> DR ALB]

  classDef control fill:#f4f7ff,stroke:#3b82f6;
  class ControlPlane control
```


## 3. Disaster Recovery Strategy

- The DR process is executed manually by an operator using an SSM Automation runbook called `DRP-Runbook-RedButton`.
- The runbook starts the Step Functions state machine `drp-workflow` (StartExecution). The Step Functions workflow orchestrates a sequence of Lambda tasks to validate, promote, deploy, health-check, and update DNS to cut traffic to DR.
- Failback is manual: this repository includes a documented runbook (FAILBACK.md) that explains how to create a cross-region replica and promote it back in the primary region.


## 4. Infrastructure Components

Provisioned resources (via Terraform):

- Lambda functions (Python): `validate_ami_tag`, `check_rds_replica`, `promote_rds_replica`, `deploy_ec2_instances`, `check_targetgroup_health`, `update_route53_failover`.
- IAM roles and inline policies for the Lambdas.
- AWS Step Functions state machine `drp-workflow`.
- AWS Systems Manager Automation document `DRP-Runbook-RedButton` and IAM role/policy for the runbook.
- Terraform packaging using `archive_file` to zip Lambda source files.


## 5. Step Functions Workflow

State machine name: `drp-workflow` (STANDARD)

Orchestration sequence (exactly as implemented):

1. Validate AMI (`validate_ami_tag`)
2. Validate RDS read replica (`check_rds_replica`)
3. Promote RDS read replica (`promote_rds_replica`)
4. Deploy EC2 instances in DR (`deploy_ec2_instances`)
5. Validate instance initialization / health (`check_targetgroup_health`)
6. Update Route53 to point to DR ALB (`update_route53_failover`)

Each step is invoked as a Lambda Task integration. Specific retry behavior and ResultPath handling are configured in the state machine definition in `stepfuntions/drp-workflow.json`.


## 6. Lambda Functions Description

Below are the Lambdas with their purpose, AWS services they call, and expected input/output within the workflow.

- `validate_ami_tag`
  - Purpose: Find the most recent AMI in the DR region with tag `DR_READY=true` and ensure it is available.
  - AWS services: Amazon EC2 `describe_images`.
  - Expected input: optional `Owner` or other search params; default region is `us-west-2`.
  - Output: `{ "amiExists": true/false, "amiId": "ami-...", "creationDate": "..." }` or raises `AMI_NOT_FOUND` error for the workflow to stop.

- `check_rds_replica`
  - Purpose: Verify that the RDS read replica (in DR region) is `available` and that replication lag is acceptable.
  - AWS services: Amazon RDS `describe_db_instances`.
  - Expected input: `ReplicaIdentifier` or `DBInstanceIdentifier`.
  - Behavior: returns replica status; raises `ReplicaNotReady` to trigger Step Functions retry if not `available`; raises `ReplicaLagTooHigh` when ReplicaLag > 30 seconds.
  - Output: `{ "replicaAvailable": true/false, "replicaStatus": "available|...", "dbInstanceIdentifier": "..." }

- `promote_rds_replica`
  - Purpose: Promote a DR read replica to primary (existing behavior). Additionally, supports a manual `create_replica` mode to create a read replica (for manual failback preparations).
  - AWS services: Amazon RDS `promote_read_replica`, `create_db_instance_read_replica`, `describe_db_instances`.
  - Expected input (promotion): `DBInstanceIdentifier` (replica id), optional `Region`.
  - Additional input (create replica): `mode: "create_replica"`, optional `SourceDBInstanceIdentifier`, optional `NewDBInstanceIdentifier`.
  - Output: On success returns `status: success` and `instance` details for the promoted or created instance. On errors returns `status: error` with code/message.

- `deploy_ec2_instances`
  - Purpose: Create a Launch Template and Auto Scaling Group (ASG) in the DR region using the validated AMI and desired instance count (workflow uses count=4).
  - AWS services: EC2 (`create_launch_template`, `describe_instances`), Auto Scaling (`create_auto_scaling_group`, `describe_auto_scaling_groups`).
  - Expected input: `AmiId`, `InstanceCount` (default 4), `InstanceType`, `SecurityGroupIds`, `SubnetIds` (or `SubnetId`), `KeyName`, `TargetGroupArn`, `Name` (tag).
  - Output: `{ "asgName": "...", "launchTemplateId": "...", "desiredCapacity": N, "instanceIds": ["i-...", ...] }` when instances are observed `InService` in the ASG.
  - Note: The Lambda waits and polls the ASG for instances to reach `InService` (configurable via environment variables in the Lambda).

- `check_targetgroup_health`
  - Purpose: Validate that target group health for the provided instance IDs is `healthy` in the ALB target group.
  - AWS services: ELBv2 `describe_target_health`.
  - Expected input: `InstanceIds` (list) and `TargetGroupArn`.
  - Output: `{ "allHealthy": true/false, "healthy": [...], "unhealthy": [...], "details": {...} }`. Raises `UnhealthyTargets` to trigger retries if not all healthy.

- `update_route53_failover`
  - Purpose: Perform a Route53 `UPSERT` for the application's DNS record to point to the DR ALB (alias) and wait for the Route53 change to be submitted.
  - AWS services: ELBv2 `describe_load_balancers` (to discover ALB DNS/zone), Route53 `ChangeResourceRecordSets`.
  - Expected input: `HostedZoneId`, `RecordName`, and either `TargetLoadBalancerDnsName` or `LoadBalancerArn`.
  - Output: returns changeInfo from Route53 and `recordName` on success.


## 7. Manual DR Trigger (Red Button)

- SSM Automation document name: `DRP-Runbook-RedButton`.
- Purpose: Provide a single-button manual operation that starts the `drp-workflow` Step Functions state machine.
- How it works: the automation document uses the `aws:executeAwsApi` action to call `states:StartExecution` against the Step Functions state machine `drp-workflow` and accepts an `ExecutionInput` parameter for the JSON payload.

How an engineer triggers DR manually:

1. In the AWS Console navigate to Systems Manager > Automation.
2. Find `DRP-Runbook-RedButton` and start an automation execution.
3. Provide a JSON `ExecutionInput` matching the Step Functions input structure (or use `{}` for defaults). The runbook assumes a role that has `states:StartExecution` permission on the `drp-workflow` state machine.

CLI example (start automation execution):

```bash
aws ssm start-automation-execution \
  --document-name "DRP-Runbook-RedButton" \
  --parameters ExecutionInput='{"DBInstanceIdentifier":"replica-id","TargetGroupArn":"arn:..."}'
```

Or invoke the Step Functions state machine directly (if you prefer):

```bash
aws stepfunctions start-execution \
  --state-machine-arn <state-machine-arn> \
  --input '{"DBInstanceIdentifier":"replica-id","TargetGroupArn":"arn:..."}'
```


## 8. Failover Execution Flow (Detailed)

1. Validate AMI: `validate_ami_tag` searches for `DR_READY=true` AMIs in `us-west-2` and enforces AMI state `available`. If missing, the workflow stops.
2. Validate RDS read replica: `check_rds_replica` verifies the replica is `available` and checks replication lag (fails if lag exceeds threshold).
3. Promote RDS read replica: `promote_rds_replica` promotes the replica to primary (the Lambda now also supports a manual `create_replica` mode, but promotion is the default path in the workflow).
4. Deploy EC2 instances: `deploy_ec2_instances` creates a Launch Template + ASG configured for the validated AMI and desired instance count (workflow uses 4 instances). The Lambda returns the created `instanceIds` after instances reach `InService`.
5. Health check: `check_targetgroup_health` confirms all instances are `healthy` in the ALB target group (with retries configured in the state machine).
6. Update DNS: `update_route53_failover` performs a Route53 `UPSERT` to point the application record to the DR ALB (Alias) and returns the Route53 change info.

When all steps succeed the Step Function completes and traffic is directed to DR.


## 9. Failback Strategy — Returning traffic to the Primary Region

This repository supports failover to the DR region. Failback (returning traffic from DR back to the primary region) is a separate, deliberate operation and should be executed only after the primary region is verified healthy. The following describes the recommended, documented failback process (documentation only — this does not change any automated workflow):

Failback overview (high level):

1. Validate Primary Region Recovery
  - Ensure the primary region (`us-east-1`) infrastructure (networking, ALB, VPC, subnets) is healthy and that the primary RDS instance is reachable from the application tier.
  - Confirm the root cause of the primary outage is resolved and that no ongoing events will immediately cause re-failure.

2. Recreate Replication from DR → Primary
  - Create a cross-region read replica in `us-east-1` from the active DR primary (the promoted replica in `us-west-2`). Use `create-db-instance-read-replica` or logical replication/pg_dump/restore per your recovery plan.
  - Monitor replication until the replica in `us-east-1` reaches `available` and replication lag is acceptable.

3. Promote the Primary-Region Replica to Master
  - Once the `us-east-1` replica is caught up and validated, promote it to primary using `promote-read-replica` (or equivalent). This makes the original primary region database authoritative again.
  - Perform post-promotion validation (schema/data integrity checks, connection tests).

4. Rebuild Application Instances in Primary Region
  - Recreate or scale up EC2 application instances in `us-east-1` using the validated AMI (or the same launch template/ASG used previously). Ensure instances are registered to the primary ALB Target Group and reach `InService`.
  - If you used an ASG with desired capacity 0 during failover, scale it to the previous production capacity or create instances per runbook.

5. Validate ALB Target Health in Primary Region
  - Verify all primary-region instances are `healthy` in the ALB Target Group (health checks passing). Use the `check_targetgroup_health` Lambda or console to confirm.

6. Switch Route53 Traffic Back to Primary
  - Update Route53 to point the application record to the primary ALB (alias A) using `update_route53_failover` or the console.
  - Verify traffic flows to primary and monitor for errors.

7. Decommission Temporary DR Infrastructure
  - After a successful failback and verification window, scale down or remove temporary DR instances and ASG capacity if they were created only for the failover.
  - If the DR replica was promoted to a temporary primary, reconcile replication topology to restore the original replication topology (create new read replicas if desired).

Notes and safeguards:

- The promoted RDS instance in `us-west-2` is a temporary primary during failover — see "Temporary DR Database Behavior" for details.
- Failback is a manual, multi-step process; validate each step and monitor closely. Consider using a separate SSM Runbook to perform failback tasks with operator confirmation at each step.
- Always perform failback in a maintenance window and notify stakeholders. Maintain runbooks and test failback in a staging copy before using in production.


## 10. Deployment (Terraform)

The repository includes a `terraform/` configuration that packages Lambdas via `archive_file`, creates IAM roles/policies, deploys Lambda functions, creates the Step Functions state machine, and adds the SSM Automation document.

Typical workflow (local engineer):

```bash
cd terraform
terraform init
terraform plan -var 'db_instance_identifier=primary-db' -var 'hosted_zone_id=ZAAAAAA' -var 'target_group_arn=arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/example/abcdef'
terraform apply -var 'db_instance_identifier=primary-db' -var 'hosted_zone_id=ZAAAAAA' -var 'target_group_arn=arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/example/abcdef'
```

Outputs available in Terraform:
- `lambda_arns` — ARNs for all deployed Lambda functions
- `state_machine_arn` — ARN for the `drp-workflow` state machine

Note: Terraform requires AWS credentials available to the CLI (environment variables, profile, or instance role) to plan and apply.


## 11. Operational Considerations

- Route53 TTL: Use a low TTL for the application record (e.g., 60s) to speed cutover/recovery.
- IAM: Lambdas require least-privilege IAM policies to access RDS, EC2, ELBv2, AutoScaling and Route53. The runbook role requires `states:StartExecution` on the `drp-workflow` state machine.
- Idempotency: Lambdas try to avoid duplicate resources (e.g., using deterministic naming when creating replicas or tags). The workflow is safe to retry for transient failures.
- Manual control: The SSM runbook provides deliberate manual control — do not automate runbook execution without approval.


## 12. Temporary DR Database Behavior

- When the DR read replica in `us-west-2` is promoted, it becomes the active primary database for the application. This promoted instance should be considered a **temporary primary** until the original primary region is recovered and a failback is performed.
- Treat the promoted DR primary as authoritative for application traffic during the DR period. Do not assume it's permanent — the runbook and operational plan should treat this as a temporary topology change.
- Once the primary region is restored, follow the Failback Strategy to rebuild replication and promote the primary-region replica back to master.


## 13. Monitoring, Observability and Alerting

This section documents recommended observability for a production-grade DR process.

- **CloudWatch Logs (Lambda)**: Enable CloudWatch Logs for each Lambda. Configure log retention and structured (JSON) logging for easier parsing. Key diagnostics to monitor:
  - Errors and stack traces
  - Lambda durations and throttles
  - Custom metrics (e.g., replication lag, AMI lookup failures)

- **Step Functions Execution History**: Use Step Functions console to inspect executions, step-level inputs/outputs, and errors. Configure detailed execution logging to CloudWatch for long-term retention and analysis.

- **Route53 Change Logs & DNS Monitoring**: Track Route53 change submissions (ChangeInfo) and monitor DNS resolution from representative locations. Consider TTL tuning and active probes to validate DNS switch.

- **EC2 / Auto Scaling Events**: Monitor ASG lifecycle events, EC2 instance state changes, and CloudWatch metrics (CPU, networking) to detect unexpected behavior after failover.

- **Alarms & Notifications**: Create CloudWatch Alarms for critical indicators and publish to SNS topics for on-call notification:
  - Failed Step Functions executions (errors) → SNS
  - Lambda error count or throttles → SNS
  - RDS replica lag above threshold → SNS
  - ALB target group unhealthy host count → SNS
  - Route53 change failures or unexpected DNS drift → SNS

- **Dashboards & Runbooks**: Create CloudWatch dashboards summarizing execution status, replica lag, target group health, and Route53 status. Keep runbooks (SSM Automation) and playbooks updated and accessible to responders.

Operational guidance: Subscribe on-call channels to SNS topics and ensure runbook owners can access the Step Functions execution history and CloudWatch logs. Use tags and structured log fields to correlate a single DR execution across services.


---

This README describes the current implementation present in this repository. For procedural details and CLI command examples for failback, see `FAILBACK.md` in the repository.
