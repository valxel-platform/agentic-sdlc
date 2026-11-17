"""Scheduled jobs: their schedules at G4, for the coder, and on every deploy (jobs.py)."""

import pytest

from valxel_sdlc import jobs
from valxel_sdlc.graph.infra import _apply_capacity

JOB_YAML = """name: farm-reminders
schedule: "15 3 * * *"
timezone: America/Chicago
time_limit_minutes: 20
environments:
  dev: { schedule: "15 * * * *" }
  staging: { suspend: true }
"""
REPOS = [{"name": "acme-farm/farm-api", "role": "provider"}, {"name": "acme-farm/farm-reminders", "role": "job"}]


@pytest.mark.parametrize("text,zone,ok", [
    ("15 3 * * *", "America/Chicago", True), ("*/15 9-17 * * 1-5", "UTC", True), ("0 0 1,15 * 7", "UTC", True),
    ("15 3 * *", "UTC", False), ("61 * * * *", "UTC", False), ("0 0 * * MON", "UTC", False),
    ("*/0 * * * *", "UTC", False), ("5-1 * * * *", "UTC", False), ("0 3 * * *", "Mars/Base", False),
])
def test_schedules_follow_the_templates_rules(text, zone, ok):
    assert (jobs.check_schedule(text, zone) == "") is ok


def test_the_design_names_each_jobs_schedule_and_why():
    hld = "## Schedules\n- acme-farm/farm-reminders: 15 3 * * * America/Chicago; sends the morning reminders\n"
    assert jobs.design_findings(hld, REPOS) == []
    [line] = jobs.parse_schedules(hld)
    assert (line["schedule"], line["timezone"]) == ("15 3 * * *", "America/Chicago")
    assert "15 3 * * *" in jobs.coder_instruction(hld, "acme-farm/farm-reminders")
    assert jobs.coder_instruction(hld, "acme-farm/farm-api") == ""


def test_missing_wrong_or_unexplained_schedules_are_refused():
    assert "say when it runs" in jobs.design_findings("", REPOS)[0]
    bad = jobs.design_findings("## Schedules\n- acme-farm/farm-reminders: 15 3 * * MON; x\n"
                               "- acme-farm/farm-api: 0 * * * *; polls every hour for news\n", REPOS)
    assert any("MON" in f for f in bad) and any("say what the job does" in f for f in bad)
    assert any("farm-api isn't a job repo" in f for f in bad)
    assert "isn't '<five cron fields>" in jobs.design_findings(
        "## Schedules\n- acme-farm/farm-reminders: daily at 3; reminders for the day\n", REPOS)[0]


def test_each_environment_gets_its_own_schedule_from_job_yaml():
    assert jobs.binding_configs(JOB_YAML, "production") == {
        "environment": "production", "schedule": "15 3 * * *", "timeZone": "America/Chicago", "suspend": False,
        "activeDeadlineSeconds": 1200}
    assert jobs.binding_configs(JOB_YAML, "dev")["schedule"] == "15 * * * *"
    assert jobs.binding_configs(JOB_YAML, "staging")["suspend"] is True
    assert jobs.binding_configs("", "dev") == {} and jobs.binding_configs("a: [", "dev") == {}
    assert jobs.binding_configs('schedule: "nope"', "dev") == {}


def test_a_deploy_sets_the_cronjob_from_job_yaml_at_the_deployed_commit(services):
    services.github.files[("acme-farm/farm-reminders", "abc123", "job.yaml")] = JOB_YAML
    target = {"component": "farm-reminders", "project": "farm", "namespace": None,
              "pipeline": ("dev", "staging", "production")}
    got = _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "production",
                          repo="acme-farm/farm-reminders", role="job", ref="abc123")
    assert got["schedule"]["schedule"] == "15 3 * * *"
    applied = services.openchoreo.binding_configs[("farm-reminders", "production")]["configs"]
    assert applied["timeZone"] == "America/Chicago" and applied["activeDeadlineSeconds"] == 1200
    # Not a job, or no job.yaml: nothing to set without a capacity plan.
    assert _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "dev",
                           repo="acme-farm/farm-api", role="provider", ref="abc123") is None


def test_the_job_role_is_registered_everywhere_roles_are_listed():
    from valxel_sdlc import acceptance, catalog_info, changeset_env, repo_roles
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.phase import SECURITY

    assert "job" in repo_roles.ROLES and "job" in changeset_env.RUNTIME_ROLES and "job" in acceptance.RUNTIME_ROLES
    assert "job" in checks.RUNTIME_ROLES and catalog_info.TYPES["job"] == "job" and "job.yaml" in SECURITY["job"]
