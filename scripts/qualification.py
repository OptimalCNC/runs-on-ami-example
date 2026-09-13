"""Keep image build provenance separate from the dispatch qualifying that image."""
import dataclasses
import os
import re

from example import build_id, match, require


@dataclasses.dataclass(frozen=True)
class QualificationRun:
    build_id: str
    run_id: str
    run_attempt: str
    workflow_ref: str

    def __post_init__(self):
        match(self.build_id, r"[1-9][0-9]*-[1-9][0-9]*-(one|two)", "qualification build")
        match(self.run_id, r"[1-9][0-9]*", "qualification run ID")
        match(self.run_attempt, r"[1-9][0-9]*", "qualification run attempt")
        require(self.build_id == build_id(self.run_id, self.run_attempt, self.build_id.rsplit("-", 1)[1]),
                "qualification build differs from run and attempt")
        require(isinstance(self.workflow_ref, str) and bool(self.workflow_ref.strip()), "qualification workflow reference is missing")

    @classmethod
    def from_result(cls, result):
        validation = result.get("validation", {})
        require(isinstance(validation, dict), "image validation must be an object")
        fields = {field.name for field in dataclasses.fields(cls)}
        if "qualification" in validation:
            value = validation["qualification"]
            require(isinstance(value, dict) and set(value) == fields, "qualification context fields differ")
        else:
            execution = result.get("execution")
            require(isinstance(execution, dict) and fields <= set(execution), "image execution context is missing")
            value = {field: execution[field] for field in fields}
        return cls(**value)

    @classmethod
    def current(cls, repository, variant="one"):
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == "refs/heads/main", "qualification dispatches require main")
        require(os.environ.get("GITHUB_REPOSITORY") == repository, "qualification dispatch repository differs")
        workflow_ref = match(os.environ.get("GITHUB_WORKFLOW_REF"),
                             re.escape(repository) + r"/\.github/workflows/[^/@]+\.ya?ml@refs/heads/main",
                             "qualification workflow reference")
        run_id, attempt = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")
        return cls(build_id(run_id, attempt, variant), run_id, attempt, workflow_ref)
