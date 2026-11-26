"""The release rehearsal's ports on each cloud (ADR 0010): DatabaseCopies and ReportStore, each
adapter against a fake of its cloud's API, and the conformance checks every implementation passes."""

from datetime import UTC, datetime
from urllib.error import HTTPError

import pytest

from valxel_sdlc.cloud import conformance, database_copies, fakes, report_store
from valxel_sdlc.cloud.aws.rehearsal import RdsCopies, S3Reports
from valxel_sdlc.cloud.azure.rehearsal import BlobReports, FlexibleServerCopies
from valxel_sdlc.cloud.gcp.rehearsal import CloudSqlCopies, GcsReports


def test_the_fakes_pass():
    assert conformance.database_copies(fakes.MemoryCopies(), source="tool-db") == []
    assert conformance.report_store(fakes.MemoryReports()) == []


def test_each_cloud_has_its_own(monkeypatch):
    assert isinstance(database_copies({"VALXEL_CLOUD": "aws", "AWS_REGION": "us-east-1"}), RdsCopies)
    assert isinstance(database_copies({"VALXEL_CLOUD": "gcp", "VALXEL_GCP_PROJECT": "p"}), CloudSqlCopies)
    assert isinstance(database_copies({"VALXEL_CLOUD": "azure", "REHEARSAL_SOURCE": "/s/x"}), FlexibleServerCopies)
    assert isinstance(report_store("b", env={"VALXEL_CLOUD": "aws"}), S3Reports)
    assert isinstance(report_store("gs://b", env={"VALXEL_CLOUD": "gcp"}), GcsReports)
    assert isinstance(report_store("https://a.blob.core.windows.net/r", env={"VALXEL_CLOUD": "azure"}), BlobReports)
    with pytest.raises(ValueError):
        report_store("not-a-container-url", env={"VALXEL_CLOUD": "azure"})


# ---------------------------------------------------------------- AWS


class FakeRds:
    class DBInstanceNotFoundFault(Exception):
        pass

    def __init__(self):
        self.dbs = {"valxel-tools-sdlc": self._db("valxel-tools-sdlc", "db.t4g.medium")}
        self.calls = []

    @staticmethod
    def _db(name, cls):
        return {"DBInstanceIdentifier": name, "DBInstanceClass": cls, "DBInstanceStatus": "available",
                "Endpoint": {"Address": f"{name}.abc.us-east-1.rds.amazonaws.com", "Port": 5432},
                "MasterUsername": "postgres_admin", "DBSubnetGroup": {"DBSubnetGroupName": "tools-db"},
                "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1"}],
                "DBParameterGroups": [{"DBParameterGroupName": "pg16"}]}

    def describe_db_instances(self, DBInstanceIdentifier):
        if DBInstanceIdentifier not in self.dbs:
            raise self.DBInstanceNotFoundFault(DBInstanceIdentifier)
        return {"DBInstances": [self.dbs[DBInstanceIdentifier]]}

    def restore_db_instance_to_point_in_time(self, **kw):
        self.calls.append(("restore", kw))
        self.dbs[kw["TargetDBInstanceIdentifier"]] = self._db(kw["TargetDBInstanceIdentifier"], kw["DBInstanceClass"])

    def modify_db_instance(self, **kw):
        self.calls.append(("modify", kw))

    def delete_db_instance(self, **kw):
        self.calls.append(("delete", kw))
        if self.dbs.pop(kw["DBInstanceIdentifier"], None) is None:
            raise self.DBInstanceNotFoundFault(kw["DBInstanceIdentifier"])


def test_rds_copies_beside_the_source(monkeypatch):
    monkeypatch.setattr("valxel_sdlc.cloud.aws.rehearsal.time.sleep", lambda s: None)
    rds = FakeRds()
    copies = RdsCopies("us-east-1", client=rds, instance_class="db.t4g.small")
    assert conformance.database_copies(copies, source="valxel-tools-sdlc", name="valxel-rehearsal-sdlc") == []
    restore = dict(rds.calls)["restore"]
    assert restore["UseLatestRestorableTime"] and restore["DBInstanceClass"] == "db.t4g.small"
    assert (restore["DBSubnetGroupName"], restore["VpcSecurityGroupIds"]) == ("tools-db", ["sg-1"])
    assert not restore["MultiAZ"] and not restore["PubliclyAccessible"] and not restore["DeletionProtection"]
    modify = dict(rds.calls)["modify"]
    assert modify["ManageMasterUserPassword"] is False and modify["ApplyImmediately"]


# ---------------------------------------------------------------- Google Cloud


class FakeCloudSql:
    def __init__(self):
        self.instances = {"valxel-tools-sdlc": {"settings": {"deletionProtectionEnabled": True},
                                                "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.20.0.5"}]}}
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "operations":
            return {"name": parts[1], "status": "DONE"}
        name = parts[1]
        if method == "POST" and parts[2] == "clone":
            new = body["cloneContext"]["destinationInstanceName"]
            self.instances[new] = {"settings": dict(self.instances[name]["settings"]),
                                   "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.20.0.9"}]}
            return {"name": "op-clone", "status": "RUNNING"}
        if name not in self.instances:
            raise HTTPError(path, 404, "not found", {}, None)
        if method == "PATCH":
            self.instances[name]["settings"].update(body["settings"])
        if method == "DELETE":
            if self.instances[name]["settings"].get("deletionProtectionEnabled"):
                raise HTTPError(path, 400, "deletion protection", {}, None)
            del self.instances[name]
        if method == "GET":
            return self.instances[name]
        return {"name": f"op-{method.lower()}", "status": "RUNNING"}


def test_cloud_sql_clones_and_can_delete_its_clone():
    api = FakeCloudSql()
    copies = CloudSqlCopies("valxel-tools", call=api, sleep=lambda s: None)
    got = copies.copy("valxel-tools-sdlc", "valxel-rehearsal-sdlc")
    assert got == {"host": "10.20.0.9", "port": 5432, "user": "postgres"}  # the private IP, in the network
    assert api.instances["valxel-rehearsal-sdlc"]["settings"]["deletionProtectionEnabled"] is False
    copies.delete("valxel-rehearsal-sdlc")
    assert conformance.database_copies(copies, source="valxel-tools-sdlc") == []
    assert any(m == "PUT" and "/users?name=postgres" in p for m, p, _ in api.calls)


# ---------------------------------------------------------------- Azure


SRC = ("/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/valxel-tools/providers/"
       "Microsoft.DBforPostgreSQL/flexibleServers/valxel-tools-sdlc")


class FakeArm:
    def __init__(self):
        props = {"state": "Ready", "administratorLogin": "valxeladmin",
                 "fullyQualifiedDomainName": "src.postgres.database.azure.com"}
        self.servers = {SRC: {"location": "eastus2", "properties": props}}
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.servers[path] = {"location": body["location"], "properties": {
                "state": "Ready", "fullyQualifiedDomainName": f"{name}.postgres.database.azure.com"}}
            return {}
        if path not in self.servers:
            raise HTTPError(path, 404, "not found", {}, None)
        if method == "DELETE":
            del self.servers[path]
        return self.servers[path] if method == "GET" else {}


def test_flexible_server_restores_beside_its_source():
    arm = FakeArm()
    noon = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    copies = FlexibleServerCopies(SRC, call=arm, sleep=lambda s: None, now=lambda: noon)
    got = copies.copy(SRC, "valxel-rehearsal-sdlc")
    assert got == {"host": "valxel-rehearsal-sdlc.postgres.database.azure.com", "port": 5432, "user": "valxeladmin"}
    [put] = [b for m, _, b in arm.calls if m == "PUT"]
    assert put["properties"] == {"createMode": "PointInTimeRestore", "sourceServerResourceId": SRC,
                                 "pointInTimeUTC": "2026-11-25T11:55:00Z"} and put["location"] == "eastus2"
    copies.delete("valxel-rehearsal-sdlc")
    assert conformance.database_copies(copies, source=SRC) == []
