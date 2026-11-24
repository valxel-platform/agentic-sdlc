"""Product roadmap: goals, initiatives and releases above features.

Git is the record (roadmap/ in each product's governance repo, changed by pull request); the tool
keeps the live state of initiatives and releases it is running (drafting, at their gate, waiting
for features) and computes progress from the features themselves. Trackers get a copy.
"""

from __future__ import annotations

# The roadmap record (reading it) and governance pull requests are the core's: roadmap/record.py and
# governance/prs.py. Re-exported here for the roadmap's own modules.
from ..governance.prs import governance_pr  # noqa: F401
from .record import ROADMAP_DIR, parse_goals, parse_initiative, parse_release, read_roadmap  # noqa: F401
