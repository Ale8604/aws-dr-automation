# Failback Runbook (DR -> Primary)

Este runbook describe los pasos para regresar la escritura a `us-east-1` desde la región DR (`us-west-2`). Ajusta los `ACCOUNT_ID`, `DB identifiers`, y `HostedZoneId` antes de ejecutar.

## 1) Verificar estado de la infraestructura en us-east-1

- EC2 (instancias y estado):

```bash
aws ec2 describe-instances --region us-east-1 \
  --query 'Reservations[].Instances[].{ID:InstanceId,State:State.Name,AZ:Placement.AvailabilityZone,PrivateIP:PrivateIpAddress,PublicIP:PublicIpAddress,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table
```

- RDS (instancias y endpoints):

```bash
aws rds describe-db-instances --region us-east-1 \
  --query 'DBInstances[].{ID:DBInstanceIdentifier,Status:DBInstanceStatus,Endpoint:Endpoint.Address,Role:ReadReplicaSourceDBInstanceIdentifier}' \
  --output table
```

## 2) Crear réplica de lectura cross-region (PostgreSQL)

Supongamos que la primaria activa está en `us-west-2` con identifier `primary-west` y quieres crear una réplica en `us-east-1` con id `replica-east-1`.

1) Crear la réplica cross-region (ejecutar en `us-east-1`):

```bash
aws rds create-db-instance-read-replica \
  --region us-east-1 \
  --db-instance-identifier replica-east-1 \
  --source-db-instance-identifier arn:aws:rds:us-west-2:123456789012:db:primary-west \
  --db-instance-class db.t3.medium \
  --availability-zone us-east-1a
```

Notas:
- Si la instancia fuente está cifrada, añade `--kms-key-id <kms-key-arn>` apropiado para la región de destino.
- Ajusta `--db-instance-class` y AZ según tu arquitectura.

2) Esperar a que la réplica esté disponible:

```bash
aws rds wait db-instance-available --region us-east-1 --db-instance-identifier replica-east-1
```

3) Opcional: verificar replicación y lag (Postgres):

Conéctate al endpoint restaurado y revisa `pg_stat_replication` o las vistas equivalentes según tu topología.

## 3) Promover la réplica en us-east-1 (cuando esté sincronizada)

```bash
aws rds promote-read-replica --region us-east-1 --db-instance-identifier replica-east-1

# Esperar que la nueva primaria esté `available`
aws rds wait db-instance-available --region us-east-1 --db-instance-identifier replica-east-1
```

## 4) Actualizar DNS con `update_route53_failover.py`

Hay dos maneras de ejecutar el cambio DNS usando tu script existente:

- A) Si `UpdateRoute53FailoverFunction` está desplegada como Lambda — invócala con `aws lambda invoke`:

```bash
aws lambda invoke \
  --function-name UpdateRoute53FailoverFunction \
  --region us-west-2 \
  --payload '{"HostedZoneId":"ZXXXXXXXXXXX","RecordName":"app.example.com","TargetLoadBalancerDnsName":"my-alb-123.us-east-1.elb.amazonaws.com"}' \
  response.json

cat response.json
```

- B) Ejecutar el script localmente (útil para pruebas desde tu estación con credenciales):

```bash
PYTHONPATH=. python3 - <<'PY'
from lamnbdas.update_route53_failover import lambda_handler
event={
  "HostedZoneId": "ZXXXXXXXXXXX",
  "RecordName": "app.example.com",
  "TargetLoadBalancerDnsName": "my-alb-123.us-east-1.elb.amazonaws.com"
}
print(lambda_handler(event, None))
PY
```

Notas:
- `update_route53_failover.py` busca el ALB en la región configurada por `AWS_REGION` (por defecto `us-west-2`). Si invocas localmente para cambiar a `us-east-1`, exporta `AWS_REGION=us-east-1` o asegúrate de pasar el ALB ARN en el payload.

## 5) Validaciones post-switch

- Verifica que el target group en us-east-1 esté sano (ELB/ALB health):

```bash
aws elbv2 describe-target-health --region us-east-1 --target-group-arn <target-group-arn>
```

- Ejecuta peticiones funcionales y pruebas de escritura contra el endpoint DNS (`app.example.com`).

## 6) Rollback

- Si hay problemas, re-evalúa y usa `update_route53_failover.py` para apuntar nuevamente al ALB en `us-west-2` usando el mismo proceso.

## 7) Ejemplo rápido de checklist

1. Verificar infra en us-east-1
2. Crear réplica cross-region / restaurar desde snapshot
3. Esperar sincronización / promover réplica
4. Desplegar o validar EC2 en us-east-1
5. Cambiar DNS con `update_route53_failover.py`
6. Validar healthchecks y tráfico
