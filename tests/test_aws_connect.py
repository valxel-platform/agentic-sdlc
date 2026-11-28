"""Connecting a workspace's AWS account (the sign-in design, A7): a CloudFormation stack the customer
creates from a ready-made link, whose roles trust only Valxel's account with the workspace's
external ID, then a check that assumes them."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.connections import AwsConnect, ConnectionError_, Connections, aws_health
from valxel_sdlc.web.auth import AuthConfig

WS = "wrkspc-shared"
ACCOUNT = "123456789012"
TEMPLATE = "https://valxel-public.s3.amazonaws.com/customer-stack.yaml"


class FakeSts:
    """Assume-role as AWS would answer it: only the roles the stack made, with the right external ID."""

    def __init__(self):
        self.stacks: dict[str, str] = {}  # role arn -> the external id it trusts
        self.calls = []

    def create_stack(self, conns, workspace, account):
        for arn in AwsConnect.roles(workspace, account).values():
            self.stacks[arn] = conns.external_id(workspace)

    def __call__(self, arn, *, external_id, session):
        self.calls.append((arn, external_id, session))
        if self.stacks.get(arn) != external_id:
            raise RuntimeError(f"An error occurred (AccessDenied) when calling the AssumeRole operation: "
                               f"User is not authorized to perform: sts:AssumeRole on resource: {arn}")
        return {"Account": arn.split(":")[4], "Arn": f"{arn}/{session}"}


@pytest.fixture
def sts():
    return FakeSts()


@pytest.fixture
def conns(engine, sts):
    c = Connections(engine.services.workspaces, aws=AwsConnect(valxel_account="999999999999", template_url=TEMPLATE,
                                                               assume=sts))
    engine.services.connections = c
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha", "email": "asha@farm.example"})
    return c


@pytest.fixture
def client(engine, conns):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha"})
        yield c


def test_the_launch_link_opens_awss_quick_create_with_this_workspaces_stack(client, conns):
    r = client.get(f"/api/workspaces/{WS}/connections/aws/launch", params={"mode": "lite", "region": "us-east-2"}).json()
    url = urlparse(r["url"])
    assert url.netloc == "console.aws.amazon.com" and parse_qs(url.query) == {"region": ["us-east-2"]}
    route, _, query = url.fragment.partition("?")
    assert route == "/stacks/quickcreate"
    q = parse_qs(query)
    assert q["templateURL"] == [TEMPLATE] and q["stackName"] == [f"valxel-{WS}"]
    assert q["param_ValxelAccountId"] == ["999999999999"] and q["param_WorkspaceId"] == [WS] and q["param_Mode"] == ["lite"]
    xid = q["param_ExternalId"][0]
    assert xid.startswith(f"valxel-{WS}-") and len(xid) > 40
    assert conns.external_id(WS) == xid  # made once, kept on the workspace
    other = conns.external_id("acme-other")
    assert other != xid
    assert "connection.aws_stack_opened" in [a["action"] for a in client.get(f"/api/workspaces/{WS}/audit").json()]
    r = client.get(f"/api/workspaces/{WS}/connections/aws/launch", params={"mode": "self-hosted"})
    assert r.status_code == 409 and "lite or full" in r.json()["detail"]


def test_checking_assumes_both_roles_with_the_external_id_and_records_the_account(client, conns, sts):
    sts.create_stack(conns, WS, ACCOUNT)
    r = client.post(f"/api/workspaces/{WS}/connections/aws/check", json={"account": "1234-5678-9012", "mode": "lite"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["account"] == ACCOUNT and c["status"] == "connected"
    assert c["reach"] == {"account_id": ACCOUNT, "mode": "lite",
                          "deploy_role": f"arn:aws:iam::{ACCOUNT}:role/valxel/valxel-{WS}-deploy",
                          "agents_role": f"arn:aws:iam::{ACCOUNT}:role/valxel/valxel-{WS}-agents"}
    assert {x[1] for x in sts.calls} == {conns.external_id(WS)} and len(sts.calls) == 2
    page = {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}
    assert page["aws"]["status"] == "connected" and page["aws"]["available"] is True


@pytest.mark.parametrize("account, setup, why", [
    (ACCOUNT, None, "the stack isn't there"),                       # not created yet, failed or deleted
    (ACCOUNT, "other-workspace", "the stack isn't there"),          # made for another workspace's external ID
    ("12345", None, "12 digits"),
])
def test_a_check_that_fails_says_why_in_plain_words(client, conns, sts, account, setup, why):
    if setup == "other-workspace":
        for arn in AwsConnect.roles(WS, ACCOUNT).values():
            sts.stacks[arn] = conns.external_id("someone-else")
    r = client.post(f"/api/workspaces/{WS}/connections/aws/check", json={"account": account.ljust(12, "x")[:14]
                                                                          if account == "12345" else account})
    assert r.status_code == 422 and why in r.json()["detail"]
    assert {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}["aws"]["status"] == "not_connected"


def test_a_role_answering_from_another_account_is_refused(conns):
    aws = AwsConnect(valxel_account="9" * 12, template_url=TEMPLATE, assume=lambda arn, **kw: {"Account": "000000000000"})
    with pytest.raises(ConnectionError_, match="answered from account 000000000000"):
        aws.check(WS, account=ACCOUNT, external_id="x")


def test_only_admins_connect_aws(client, engine):
    _, token = engine.services.workspaces.invite(WS, {"github_login": "vic"}, role="member", actor="asha")
    engine.services.workspaces.accept(WS, token, login="vic")
    client.post("/api/auth/dev-login", json={"login": "vic"})
    assert client.get(f"/api/workspaces/{WS}/connections/aws/launch", params={"mode": "lite"}).status_code == 403
    assert client.post(f"/api/workspaces/{WS}/connections/aws/check", json={"account": ACCOUNT}).status_code == 403


def test_a_deleted_stack_shows_within_the_hour(conns, sts):
    sts.create_stack(conns, WS, ACCOUNT)
    conns.aws_check(WS, account=ACCOUNT, mode="full", actor="asha")
    conns.health_checks["aws"] = aws_health(conns)
    assert conns.check(WS) == []
    sts.stacks.clear()  # the customer deleted the stack
    [c] = conns.check(WS)
    assert c["status"] == "needs_attention" and "the stack isn't there" in c["last_error"]


class _CfnLoader(yaml.SafeLoader):
    pass


for _tag in ("!Ref", "!Sub", "!GetAtt", "!If", "!Equals", "!Not", "!Join"):
    _CfnLoader.add_multi_constructor(_tag, lambda loader, suffix, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
        loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node)))
    _CfnLoader.add_constructor(_tag, lambda loader, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
        loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node)))


def test_every_role_trusts_only_valxel_with_the_external_id_and_acts_only_on_tagged_resources():
    t = yaml.load((Path(__file__).parent.parent / "deploy" / "customer-stack.yaml").read_text(), Loader=_CfnLoader)
    roles = {k: v["Properties"] for k, v in t["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    assumed_by_valxel = {k for k, p in roles.items()
                         if any("AWS" in s["Principal"] for s in p["AssumeRolePolicyDocument"]["Statement"])}
    assert assumed_by_valxel == {"AgentsRole", "DeployRoleLite", "DeployRoleFull"}
    for k in assumed_by_valxel:
        [stmt] = roles[k]["AssumeRolePolicyDocument"]["Statement"]
        assert stmt["Principal"]["AWS"].endswith(":iam::${ValxelAccountId}:root")
        assert stmt["Condition"] == {"StringEquals": {"sts:ExternalId": "ExternalId"}}
        assert roles[k]["Path"] == "/valxel/" and roles[k]["MaxSessionDuration"] == 3600
    # Agents see metadata, never data: ViewOnlyAccess, not ReadOnlyAccess.
    assert [a.rsplit("/", 1)[-1] for a in roles["AgentsRole"]["ManagedPolicyArns"]] == ["ViewOnlyAccess"]
    for k in ("DeployRoleLite", "DeployRoleFull", "CrossplaneRole"):
        stmts = [s for p in roles[k]["Policies"] for s in p["PolicyDocument"]["Statement"]]
        changing = [s for s in stmts if s.get("Sid") not in ("Look", "WhatANewVmUses", "TagOnlyWhenCreating",
                                                                   "PassOnlyThisStacksRoles")]
        assert changing and all("valxel:workspace" in str(s.get("Condition")) for s in changing), k
    assert t["Parameters"]["ExternalId"]["NoEcho"] is True


def test_a_known_account_is_named_and_valxel_checks_it_by_itself(client, conns, sts):
    r = client.get(f"/api/workspaces/{WS}/connections/aws/launch",
                   params={"mode": "full", "region": "us-east-1", "account": ACCOUNT}).json()
    assert r["sign_in"] == f"Sign in to AWS account {ACCOUNT} before you create the stack"
    poll = f"/api/workspaces/{WS}/connections/aws/poll"
    assert client.get(poll, params={"account": ACCOUNT}).json()["status"] == "waiting"
    sts.create_stack(conns, WS, ACCOUNT)
    done = client.get(poll, params={"account": "1234-5678-9012"}).json()
    assert done["status"] == "connected" and done["connection"]["reach"]["mode"] == "full"
