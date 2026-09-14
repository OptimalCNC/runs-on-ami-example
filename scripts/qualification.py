"""Identify a qualification independently of its image's original build."""
import dataclasses

from example import match, require


@dataclasses.dataclass(frozen=True)
class QualificationRun:
    build_id: str

    def __post_init__(self):
        match(self.build_id, r"[1-9][0-9]*-[1-9][0-9]*-(one|two)", "qualification build")

    @property
    def run_id(self):
        return self.build_id.split("-")[0]

    @property
    def run_attempt(self):
        return self.build_id.split("-")[1]

    @classmethod
    def parse(cls, build_id):
        return cls(build_id)

    @classmethod
    def from_result(cls, result):
        validation = result.get("validation", {})
        require(isinstance(validation, dict), "image validation must be an object")
        if "qualification" in validation:
            value = validation["qualification"]
            require(isinstance(value, dict) and "build_id" in value
                    and set(value) <= {"build_id", "run_id", "run_attempt", "workflow_ref"},
                    "qualification context fields differ")
        else:
            value = result.get("execution")
            require(isinstance(value, dict) and "build_id" in value, "image execution context is missing")
        context = cls.parse(value["build_id"])
        for field in ("run_id", "run_attempt"):
            if field in value:
                require(value[field] == getattr(context, field), f"qualification build differs from {field}")
        return context
