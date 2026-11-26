"""AWS for the release rehearsal: an RDS point-in-time copy, and its report in S3.

The copy is restored to the latest restorable time (minutes old) beside its source: the same
subnet group, security groups, parameter group and KMS key, single-AZ, no deletion protection,
tagged purpose=release-rehearsal. Its password is set after the restore and turns off a managed
master password, if the restore kept one."""

from __future__ import annotations

import secrets
import time
from typing import Any


class RdsCopies:
    def __init__(self, region: str, *, client: Any = None, instance_class: str = "") -> None:
        self.region, self._client, self.instance_class = region, client, instance_class

    @property
    def rds(self) -> Any:
        if self._client is None:
            import boto3  # the 'aws' extra

            self._client = boto3.client("rds", region_name=self.region)
        return self._client

    def _wait(self, name: str) -> dict:
        for _ in range(240):  # two hours
            db = self.rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
            if db["DBInstanceStatus"] == "available" and not db.get("PendingModifiedValues"):
                return db
            time.sleep(30)
        raise TimeoutError(f"{name} wasn't available within two hours")

    def copy(self, source: str, name: str) -> dict:
        src = self.rds.describe_db_instances(DBInstanceIdentifier=source)["DBInstances"][0]
        self.rds.restore_db_instance_to_point_in_time(
            SourceDBInstanceIdentifier=source, TargetDBInstanceIdentifier=name, UseLatestRestorableTime=True,
            DBInstanceClass=self.instance_class or src["DBInstanceClass"],
            DBSubnetGroupName=src["DBSubnetGroup"]["DBSubnetGroupName"],
            VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]],
            DBParameterGroupName=src["DBParameterGroups"][0]["DBParameterGroupName"],
            MultiAZ=False, PubliclyAccessible=False, DeletionProtection=False,
            Tags=[{"Key": "purpose", "Value": "release-rehearsal"}])
        db = self._wait(name)
        return {"host": db["Endpoint"]["Address"], "port": db["Endpoint"].get("Port", 5432),
                "user": db["MasterUsername"]}

    def set_password(self, name: str, user: str) -> str:
        password = secrets.token_urlsafe(24)
        self.rds.modify_db_instance(DBInstanceIdentifier=name, MasterUserPassword=password,
                                    ManageMasterUserPassword=False, ApplyImmediately=True)
        time.sleep(20)  # the change shows as pending only after a moment
        self._wait(name)
        return password

    def delete(self, name: str) -> None:
        try:
            self.rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True,
                                        DeleteAutomatedBackups=True)
        except Exception as exc:  # absent or already going is the same as done
            if type(exc).__name__ not in ("DBInstanceNotFoundFault", "InvalidDBInstanceStateFault"):
                raise


class S3Reports:
    def __init__(self, bucket: str, *, region: str = "", client: Any = None) -> None:
        self.bucket, self.region, self._client = bucket, region, client

    @property
    def s3(self) -> Any:
        if self._client is None:
            import boto3  # the 'aws' extra

            self._client = boto3.client("s3", region_name=self.region or None)
        return self._client

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
