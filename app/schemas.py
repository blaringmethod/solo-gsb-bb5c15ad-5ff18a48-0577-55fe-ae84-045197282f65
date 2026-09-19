"""请求/响应模型与输入校验。"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .rfc1982 import MAX_SERIAL

Serial = Annotated[int, Field(ge=0, le=MAX_SERIAL, description="32 位无符号序列号")]

# DNS 区域名：标签由字母/数字/连字符组成，允许末尾根点
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ZONE_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}?\.?$|^{_LABEL}\.?$")


def validate_zone(value: str) -> str:
    """去除首尾空白并校验 DNS 区域名，非法时抛 ValueError。"""
    zone = value.strip()
    if not zone or not _ZONE_RE.match(zone):
        raise ValueError("非法的 DNS 区域名")
    return zone


class AuditCreate(BaseModel):
    """提交一次审计：区域名 + 按观测时间升序排列的序列号列表。"""

    zone: str = Field(min_length=1, max_length=253, examples=["example.com"])
    serials: list[Serial] = Field(
        min_length=1,
        max_length=4096,
        description="按观测时间从早到晚排列的 SOA serial",
        examples=[[2026091800, 2026091801, 2026091801, 2026091705]],
    )

    @field_validator("zone")
    @classmethod
    def _check_zone(cls, value: str) -> str:
        return validate_zone(value)


class AuditItem(BaseModel):
    """单个观测点的判定结果。"""

    index: int
    serial: int
    relation: Literal["initial", "advance", "equal", "regression", "incomparable"]
    delta: int | None = Field(
        description="相对上一观测值的带符号距离；首项与不可比较时为 null"
    )
    slave_reject_risk: bool = Field(
        description="该节点是否可能使从服务器拒绝/无法应用更新"
    )
    note: str


class AuditSummary(BaseModel):
    total: int
    advance: int
    equal: int
    regression: int
    incomparable: int
    risk_indexes: list[int] = Field(description="存在从服务器拒绝风险的观测点下标")


class AuditOut(BaseModel):
    id: int
    zone: str
    created_at: str
    serials: list[int]
    items: list[AuditItem]
    summary: AuditSummary


class AuditListItem(BaseModel):
    id: int
    zone: str
    created_at: str
    total: int
    risk_count: int


class RepairPlanItem(BaseModel):
    """修复预演中单个观测点的处理结果。"""

    index: int
    original: int = Field(description="原始观测序列号")
    effective: int = Field(description="修复后生效的序列号（32 位无符号，模 2^32 回绕）")
    action: Literal["keep", "replace"] = Field(description="keep=保留原值，replace=替换为上一修复后值加 1")
    reason: str


class RepairPlanSummary(BaseModel):
    total: int
    replaced: int = Field(description="被替换的观测点数量")


class RepairPlanOut(BaseModel):
    id: int
    zone: str
    created_at: str
    items: list[RepairPlanItem]
    summary: RepairPlanSummary


class NextSerialOut(BaseModel):
    current: int
    next: int
    date: str = Field(description="建议所基于的日期（UTC，YYYYMMDD）")
    note: str


class SerialReservationCreate(BaseModel):
    """序列号预留请求：当前 serial + 幂等键。"""

    current: Serial = Field(
        description="调用方已持有的最新 serial（区域首次预留时为其当前发布值）"
    )
    request_id: str = Field(
        min_length=1,
        max_length=255,
        description="幂等键：同一区域内相同 request_id 的重试返回首次预留结果",
        examples=["deploy-20260919-001"],
    )

    @field_validator("request_id")
    @classmethod
    def _check_request_id(cls, value: str) -> str:
        request_id = value.strip()
        if not request_id:
            raise ValueError("request_id 不能为空")
        return request_id


class SerialReservationOut(BaseModel):
    id: int
    zone: str
    request_id: str
    current: int = Field(description="首次请求提交的 current 值")
    serial: int = Field(description="服务端原子分配并持久化的下一 serial")
    date: str = Field(description="分配所基于的日期（UTC，YYYYMMDD）")
    created_at: str
    replayed: bool = Field(
        default=False, description="true 表示本次为幂等重试，返回的是首次预留结果"
    )


class ReleasePlanNode(BaseModel):
    """多权威发布规划中的一个节点及其当前 serial。"""

    name: str = Field(
        min_length=1, max_length=253, examples=["ns1.example.com"], description="节点名（请求内唯一）"
    )
    serial: Serial = Field(description="节点当前持有的 SOA serial（沿用 32 位无符号校验）")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("节点名不能为空")
        return name


class ReleasePlanCreate(BaseModel):
    """多权威节点共同发布规划请求（仅规划，不写库）。"""

    nodes: list[ReleasePlanNode] = Field(
        min_length=1,
        max_length=256,
        description="参与本次发布的权威节点及其当前 serial（节点名须唯一）",
    )
    today: date | None = Field(
        default=None,
        description="规划所基于的日期（YYYY-MM-DD），缺省为服务器 UTC 当日",
    )

    @model_validator(mode="after")
    def _check_unique_names(self) -> "ReleasePlanCreate":
        seen: set[str] = set()
        duplicates: list[str] = []
        for node in self.nodes:
            if node.name in seen and node.name not in duplicates:
                duplicates.append(node.name)
            seen.add(node.name)
        if duplicates:
            raise ValueError(f"节点名须唯一，重复节点名: {', '.join(duplicates)}")
        return self


class ReleasePlanNodeResult(BaseModel):
    """候选 serial 相对单个节点的判定。"""

    name: str
    serial: int = Field(description="节点当前 serial")
    relation: Literal["advance"] = Field(description="候选相对该节点的关系（共同候选必为有效前进）")
    forward_distance: int = Field(
        ge=1, le=2**31 - 1, description="从节点当前 serial 到候选值的模 2^32 前向距离"
    )


class CandidateWindow(BaseModel):
    start: int = Field(description="当日候选区间起点 YYYYMMDD00")
    end: int = Field(description="当日候选区间终点 YYYYMMDD99")


class ReleasePlanOut(BaseModel):
    zone: str
    date: str = Field(description="规划所基于的日期（YYYYMMDD）")
    candidate: int = Field(description="候选区间中首个对所有节点均为有效前进的 serial")
    candidate_window: CandidateWindow
    nodes: list[ReleasePlanNodeResult] = Field(
        description="候选 serial 相对每个节点的关系与前向距离（与请求顺序一致）"
    )
    note: str


class ReleasePlanBlocker(BaseModel):
    """无共同候选时单个节点对当日候选区间的阻断摘要。"""

    name: str
    serial: int = Field(description="节点当前 serial")
    advance: int = Field(ge=0, le=100, description="候选区间中对该节点为有效前进的值数")
    equal: int = Field(ge=0, le=100, description="候选区间中与该节点相等的值数")
    regression: int = Field(ge=0, le=100, description="候选区间中对该节点为倒退的值数")
    incomparable: int = Field(ge=0, le=100, description="候选区间中与该节点不可比较的值数")


class ReleasePlanConflictDetail(BaseModel):
    """无共同候选时 409 响应的 detail 结构。"""

    reason: Literal["no_common_candidate", "date_out_of_range"]
    message: str
    date: str = Field(description="规划所基于的日期（YYYYMMDD）")
    candidate_window: CandidateWindow | None = Field(
        default=None, description="当日候选区间；日期超出 32 位表达范围时为 null"
    )
    checked: int = Field(default=0, description="扫描过的候选值数量（00..99 共 100 个）")
    blockers: list[ReleasePlanBlocker] = Field(
        default_factory=list, description="各节点对候选区间的 equal/regression/incomparable 阻断摘要"
    )


class ReleasePlanConflictResponse(BaseModel):
    """无共同候选时 409 响应体（HTTPException 的标准 detail 包装）。"""

    detail: ReleasePlanConflictDetail
