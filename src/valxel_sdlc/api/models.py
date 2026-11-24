"""Request bodies the API accepts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeatureIn(BaseModel):
    product_id: str = Field(examples=["CLN"])
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    budget_usd: float | None = Field(default=None, gt=0)
    simulate_faults: dict[str, int] = Field(default_factory=dict,
                                            description="Stub mode only: phase -> defective attempts")
    not_duplicate_reason: str = Field("", max_length=1000,
                                      description="Required when the request looks like an existing feature")
    initiative_id: str = Field("", description="The approved initiative this feature delivers; empty = unplanned")
    pilot: bool = Field(False, description="Measure it in the pilot report, with the shadow modes on")
    profile: str = Field("", pattern="^(|feature|express)$",
                         description="How much of the pipeline it goes through; empty = the product's default")


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    start: str = Field(min_length=10, max_length=40)
    end: str = Field(min_length=10, max_length=40)
    n: int | None = Field(default=None, ge=0)
    k: int | None = Field(default=None, ge=0)


class InitiativeIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    request: str = Field(min_length=10, max_length=4000, description="The outcome wanted and what it involves")
    measure: str = Field("", max_length=500)
    horizon: str = Field("", pattern="^(|now|next|later)$")
    research: str = Field("quick", pattern="^(quick|thorough|own)$",
                          description="quick or thorough web research, or 'own': your research is in the request")


class ProblemIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    description: str = Field("", max_length=4000)
    component: str = Field("", max_length=100)
    severity: str = Field("sev3", pattern="^sev[1-4]$")


class StartCandidateIn(BaseModel):
    index: int = Field(ge=0)
    not_duplicate_reason: str = Field("", max_length=1000)


class InceptionIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    org: str = Field("", max_length=100, pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9-]*$")
    vision: str = Field(min_length=20, max_length=8000, description="The idea: the problem, for whom, and what changes")
    goals: list[str] = Field(default_factory=list, description="'<outcome> (measure: <metric with a number>)'")
    users: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    budget_usd: float = Field(0.0, ge=0)
    product_id: str = Field("", pattern=r"^$|^[A-Z]{2,6}$", description="An existing product to describe, or a proposed id")
    workspace: str = Field("", max_length=40, description="A new product in this workspace, decided by its Product group")
    research: str = Field("quick", pattern="^(quick|thorough|own)$",
                          description="quick or thorough web research, or 'own': your research is in the vision")


class ReleaseIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    model: str = Field("launch", pattern="^(launch|train)$")
    features: list[str] = Field(min_length=1)
    target_date: str = Field("", max_length=20)
    initiative: str = ""


class NotifyIn(BaseModel):
    email: str | None = Field(default=None, max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    needed: bool | None = None
    staging: bool | None = None


class WatchIn(BaseModel):
    on: bool = True


class ItemRef(BaseModel):
    feature_id: str
    interrupt_id: str


class ReadTimeIn(BaseModel):
    seconds: float = Field(gt=0, le=60, description="Seconds the decision's page was open and in view since the "
                           "last report")


class BatchIn(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=50)
    active_minutes: float = Field(0.0, ge=0)


class RequestIn(BaseModel):
    description: str = Field(min_length=10, max_length=8000)


class StartIn(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=50)
    order: str = Field("together", pattern="^(together|sequence)$")


class LineComment(BaseModel):
    quote: str = Field("", max_length=1000, description="The line the comment is about, as written")
    text: str = Field(min_length=1, max_length=2000)


class ShipIn(BaseModel):
    release_id: str = Field("", max_length=120, description="An open release to ship with, or \"\" for on its own")


class DecisionIn(BaseModel):
    decision: str = Field(examples=["approve", "revise", "takeover", "handback", "resolved", "retry", "abort"])
    feedback: str = ""
    active_minutes: float = Field(default=0.0, ge=0)
    budget_usd: float | None = Field(default=None, gt=0, description="Escalations only: raise the feature budget")
    artifact_content: str | None = Field(default=None, description="Escalations and hand-backs: the person's own "
                                        "version of the phase's document")
    remember: bool = Field(False, description="Learn from this correction: propose a lesson for future features")
    lesson: str | None = Field(default=None, max_length=400, description="The lesson in the person's words "
                               "(implies remember)")
    action_ids: list[str] | None = Field(default=None, description="Incidents only: which proposed actions to approve")
    reason: str | None = Field(default=None, description="Why it's sent back (revise, retry, abort): one of the "
                               "pilot's reasons, e.g. missing_requirement, wrong_approach, quality")
    ship: str | None = Field(default=None, max_length=120, description="G1 only: how the feature ships: an open "
                             "release's id to ship with it, or \"\" to ship on its own; left out, it stays as it is")
    waiver_reason: str | None = Field(default=None, max_length=1000, description="Where separation of duties "
                                      "applies and the workspace allows a waiver: why the author approves their "
                                      "own work (no one else can). Recorded and listed for review.")
    override_reason: str | None = Field(default=None, max_length=1000, description="G6 only: release although the "
                                        "traceability matrix has gaps (or tracker drift that blocks), and why. "
                                        "Only the product's traceability approvers may.")
    retire: list[str] | None = Field(default=None, description="Initiative reviews, stop: the proposed features to "
                                     "retire (each opens a retire intent)")
    review_by: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                  description="Initiative reviews, continue: the next review date")
    target_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                    description="A release's date decision, move_date: the new date; "
                                                "the product gate: the launch date")
    must: list[str] | None = Field(default=None, max_length=100,
                                   description="The product gate: the launch's must-have features (titles)")
    ui: bool | None = Field(default=None, description="G2 only: whether UX design runs (it changes what people see)")
    comments: list[LineComment] | None = Field(default=None, max_length=50,
                                               description="Revise: comments on specific lines of the document")


class SubmitIn(BaseModel):
    pilot: bool = Field(False, description="Measure it in the pilot report, with the shadow modes on")
    profile: str = Field("", pattern="^(|feature|express)$",
                         description="How much of the pipeline it goes through; empty = the product's default")


class DevLoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=39)
    teams: list[str] = Field(default_factory=list)
    orgs: list[str] = Field(default_factory=list)
    email: str = Field(default="", max_length=254)


class WorkspaceIn(BaseModel):
    id: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=80)
    plan: str = Field(default="self-hosted", pattern=r"^(self-hosted|lite|full)$")


class PlanIn(BaseModel):
    plan: str = Field(pattern=r"^(lite|full)$")


class IntakeStartIn(BaseModel):
    workspace: str = Field("", max_length=40, description="Start a new product in this workspace (its admins only)")


class MemberIn(BaseModel):
    name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=254)
    github_login: str = Field(default="", max_length=39)
    role: str = "member"
    products: list[str] = Field(default_factory=list)


class MemberUpdate(BaseModel):
    role: str | None = None
    products: list[str] | None = None
    status: str | None = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class GroupMembers(BaseModel):
    members: list[str]


class AwsCheckIn(BaseModel):
    account: str = Field(min_length=12, max_length=14)
    mode: str = Field(default="", pattern=r"^(|lite|full)$")


class GcpCheckIn(BaseModel):
    project: str = Field(min_length=6, max_length=30)
    mode: str = Field(default="", pattern=r"^(|lite|full)$")


class AzureCheckIn(BaseModel):
    subscription: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    deploy_client_id: str = Field(default="", pattern=r"^(|[0-9a-fA-F-]{36})$")
    agents_client_id: str = Field(default="", pattern=r"^(|[0-9a-fA-F-]{36})$")
    tenant: str = Field(default="", pattern=r"^(|[0-9a-fA-F-]{36})$")
    mode: str = Field(default="", pattern=r"^(|lite|full)$")


class GatesIn(BaseModel):
    gates: dict[str, list[str]]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class DraftIn(BaseModel):
    product_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    problem: str | None = None
    users: str | None = None
    success_metric: str | None = None
    ui: bool | None = Field(default=None, description="Whether the intent changes what people see")
    experience: dict[str, str] | None = Field(default=None, description="The experience brief: where, main_task, "
                                              "context, look")
    size: str | None = Field(default=None, pattern="^(feature|initiative|product)$")
    horizon: str | None = None
    research: str | None = None
    name: str | None = Field(default=None, max_length=120)
    org: str | None = Field(default=None, max_length=100)
    budget_usd: float | None = Field(default=None, ge=0)


class SafeModeIn(BaseModel):
    note: str = Field("", max_length=2000, description="Why (entering by hand), or what was changed (a fix)")
    phases: list[str] = Field(default_factory=list, description="Phases to replay (default: the ones that tripped)")


class EvalRunIn(BaseModel):
    ref: str = Field(..., description="The config repo commit to evaluate")
    base_ref: str = Field("", description="The commit to compare with (default: the config in use)")
