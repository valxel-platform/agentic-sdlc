"""The scaffolding report: how much of the coder's output a slice generator could have written."""

import argparse

from valxel_sdlc import scaffolding
from valxel_sdlc.cli import _scaffolding


def test_new_files_shaped_like_scaffolding_count_and_the_rest_does_not():
    files = [{"path": "src/main/java/acme/crop/CropController.java", "status": "added", "additions": 60},
             {"path": "src/main/java/acme/crop/CropDto.java", "status": "added", "additions": 20},
             {"path": "src/main/resources/db/migration/V3__crop.sql", "status": "added", "additions": 10},
             {"path": "src/main/java/acme/crop/YieldRules.java", "status": "added", "additions": 40},
             {"path": "src/main/java/acme/crop/CropService.java", "status": "modified", "additions": 30},
             {"path": "old.txt", "status": "removed", "additions": 0}]
    assert scaffolding.tally("provider", files) == {"scaffold": 90, "new_other": 40, "changed": 30}
    assert scaffolding.tally("library", files)["scaffold"] == 0  # no generator shape for a library


def test_the_rule_needs_enough_tasks_and_a_large_enough_share():
    assert scaffolding.worth_a_generator({"tasks": 12, "share": 0.4})
    assert not scaffolding.worth_a_generator({"tasks": 3, "share": 0.9})
    assert not scaffolding.worth_a_generator({"tasks": 40, "share": 0.1})


def test_the_report_pairs_each_repo_with_its_coder_tokens(engine, services, capsys):
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"])
    rep = scaffolding.report(engine, "FARM")
    tokens = sum(r.output_tokens for r in services.ledger.records(v["feature_id"]) if r.role == "coder")
    assert rep["roles"] and sum(r["output_tokens"] for r in rep["roles"]) == tokens
    assert sum(r["tasks"] for r in rep["roles"]) == len(engine._state(v["feature_id"])["tasks"])
    assert all(0 <= r["share"] <= 1 and r["tokens_saved"] <= r["output_tokens"] for r in rep["roles"])
    assert scaffolding.report(engine, "FARM", since="2999-01-01")["roles"] == []
    assert _scaffolding(argparse.Namespace(product="FARM", since="", json=False), engine) == 0
    assert "| Role | Tasks |" in capsys.readouterr().out
