from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageInput:
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, input_json: Any) -> "StageInput":
        if not isinstance(input_json, dict):
            return cls(payload={})
        return cls(payload={**input_json})

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload}


@dataclass
class FollowUpPayload:
    type: str
    input_json: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> "FollowUpPayload":
        if not isinstance(payload, dict):
            return cls(type="", input_json={})
        job_type = str(payload.get("type") or "").strip()
        input_json = payload.get("input_json") if isinstance(payload.get("input_json"), dict) else {}
        extra = {k: v for k, v in payload.items() if k not in {"type", "input_json"}}
        return cls(type=job_type, input_json={**input_json}, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "input_json": {**self.input_json}}
        if self.extra:
            out.update(self.extra)
        return out


@dataclass
class StageOutput:
    output_json: dict[str, Any] = field(default_factory=dict)
    follow_up: list[FollowUpPayload] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> "StageOutput":
        if not isinstance(payload, dict):
            return cls(output_json={}, follow_up=[])
        output_json = payload.get("output_json") if isinstance(payload.get("output_json"), dict) else {}
        raw_follow_up = payload.get("follow_up") if isinstance(payload.get("follow_up"), list) else []
        follow_up = [FollowUpPayload.from_dict(row) for row in raw_follow_up]
        extra = {k: v for k, v in payload.items() if k not in {"output_json", "follow_up"}}
        return cls(output_json={**output_json}, follow_up=follow_up, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "output_json": {**self.output_json},
            "follow_up": [row.to_dict() for row in self.follow_up],
        }
        if self.extra:
            out.update(self.extra)
        return out


def parse_stage_input(input_json: Any) -> StageInput:
    return StageInput.from_dict(input_json)


def build_follow_up(*, job_type: str, input_json: dict[str, Any], **extra: Any) -> FollowUpPayload:
    return FollowUpPayload(type=str(job_type or "").strip(), input_json={**(input_json or {})}, extra={**extra})


def build_stage_output(
    *,
    output_json: dict[str, Any],
    follow_up: list[FollowUpPayload] | None = None,
    **extra: Any,
) -> StageOutput:
    return StageOutput(output_json={**(output_json or {})}, follow_up=list(follow_up or []), extra={**extra})
