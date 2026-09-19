"""DNS SOA 序列号审计 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Response

from . import db
from .rfc1982 import MAX_SERIAL
from .schemas import (
    AuditCreate,
    AuditListItem,
    AuditOut,
    NextSerialOut,
    ReleasePlanConflictResponse,
    ReleasePlanCreate,
    ReleasePlanOut,
    RepairPlanOut,
    SerialReservationCreate,
    SerialReservationOut,
    validate_zone,
)
from .repair import build_repair_plan
from .release_plan import ReleasePlanError, build_release_plan
from .service import analyze
from .suggest import SuggestionError, suggest_next_serial


def _normalize_zone(zone: str) -> str:
    """校验路径中的区域名，非法时返回 400（路径参数不适用 422 语义）。"""
    try:
        return validate_zone(zone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法的 DNS 区域名") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成数据库初始化（建表，幂等）
    db.init_db(app.state.db_path)
    yield


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="DNS SOA 序列号审计 API",
        description=(
            "提交区域名与按观测时间排列的 SOA serial，按 RFC 1982 模运算"
            "逐项判定前进/倒退/相等/不可比较，标出可能使从服务器拒绝更新的"
            "节点，并提供 YYYYMMDDnn 风格的下一序列号建议。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db_path = db_path or db.default_db_path()

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "dns-soa-serial-audit",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/audits", response_model=AuditOut, status_code=201)
    def create_audit(payload: AuditCreate) -> dict:
        """提交一次审计并返回完整判定结果。"""
        items, summary = analyze(payload.serials)
        return db.insert_audit(
            app.state.db_path, payload.zone, payload.serials, items, summary
        )

    @app.get("/api/audits", response_model=list[AuditListItem])
    def list_audits(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        """按时间倒序列出审计记录（摘要）。"""
        return db.list_audits(app.state.db_path, limit=limit, offset=offset)

    @app.get("/api/audits/{audit_id}", response_model=AuditOut)
    def get_audit(audit_id: int) -> dict:
        """按 ID 查询审计记录。"""
        record = db.get_audit(app.state.db_path, audit_id)
        if record is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        return record

    @app.get("/api/audits/{audit_id}/repair-plan", response_model=RepairPlanOut)
    def get_repair_plan(audit_id: int) -> dict:
        """基于已有审计即时生成序列修复预演（不写库）。

        首个 serial 保持不变；其后每项与上一个修复后值按 RFC 1982
        比较，有效前进则保留，否则替换为上一个修复后值加 1（模 2^32）。
        """
        record = db.get_audit(app.state.db_path, audit_id)
        if record is None:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        items, summary = build_repair_plan(record["serials"])
        return {
            "id": record["id"],
            "zone": record["zone"],
            "created_at": record["created_at"],
            "items": items,
            "summary": summary,
        }

    @app.get("/api/next-serial", response_model=NextSerialOut)
    def next_serial(
        current: int = Query(ge=0, le=MAX_SERIAL, description="当前 SOA serial"),
        today: date | None = Query(
            default=None,
            description="建议所基于的日期（YYYY-MM-DD），缺省为服务器 UTC 当日",
        ),
    ) -> dict:
        """给出 YYYYMMDDnn 风格的下一序列号建议（保证为有效前进）。

        当日编号耗尽或存在半区间歧义时返回 409。
        """
        effective_today = today or datetime.now(timezone.utc).date()
        try:
            suggested = suggest_next_serial(current, effective_today)
        except SuggestionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "current": current,
            "next": suggested,
            "date": effective_today.strftime("%Y%m%d"),
            "note": "建议值相对当前值为 RFC 1982 意义下的有效前进",
        }

    @app.post(
        "/api/zones/{zone}/serial-reservations",
        response_model=SerialReservationOut,
        status_code=201,
        responses={
            200: {"model": SerialReservationOut, "description": "幂等重试，返回首次预留结果"},
            409: {"description": "current 与服务端最新预留不一致，或编号耗尽/半区间歧义"},
        },
    )
    def reserve_serial(zone: str, payload: SerialReservationCreate, response: Response) -> dict:
        """原子预留下一个 YYYYMMDDnn serial 并持久化。

        - 同一区域内相同 ``request_id`` 重试返回首次结果（200），不再次推进；
        - ``current`` 与服务端已预留的最新值不一致时返回 409，
          响应体 ``latest_serial`` 给出最新值；
        - 当日编号耗尽 / 半区间歧义等同样返回 409。
        """
        zone_name = _normalize_zone(zone)
        try:
            record, replayed = db.reserve_serial(
                app.state.db_path,
                zone_name,
                payload.request_id,
                payload.current,
            )
        except db.ReservationError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": exc.code,
                    "message": str(exc),
                    "latest_serial": exc.latest_serial,
                },
            ) from exc
        except SuggestionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": exc.code, "message": str(exc), "latest_serial": None},
            ) from exc
        record["replayed"] = replayed
        if replayed:
            response.status_code = 200
        return record

    @app.get(
        "/api/zones/{zone}/serial-reservations",
        response_model=list[SerialReservationOut],
    )
    def list_serial_reservations(
        zone: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        """按区域查询预留记录（按预留先后升序）。"""
        zone_name = _normalize_zone(zone)
        return db.list_serial_reservations(
            app.state.db_path, zone_name, limit=limit, offset=offset
        )

    @app.post(
        "/api/zones/{zone}/release-plans",
        response_model=ReleasePlanOut,
        responses={
            409: {
                "model": ReleasePlanConflictResponse,
                "description": (
                    "当日 YYYYMMDD00–99 区间无共同候选（no_common_candidate，"
                    "detail 给出各节点 equal/regression/incomparable 阻断摘要），"
                    "或规划日期超出 32 位表达范围（date_out_of_range）"
                ),
            }
        },
    )
    def create_release_plan(zone: str, payload: ReleasePlanCreate) -> dict:
        """为多权威节点规划共同发布 serial（仅规划，不写库）。

        在当日 ``YYYYMMDD00``～``YYYYMMDD99`` 候选区间内按从小到大扫描，
        返回首个相对**每个**节点当前 serial 都属于 RFC 1982 有效前进
        （advance）的候选值，并给出候选相对每个节点的关系与前向距离。

        整个候选区间都不存在共同候选时返回 409，``detail.blockers`` 给出
        各节点对候选区间的 ``equal`` / ``regression`` / ``incomparable``
        阻断计数（另附 ``advance`` 计数，四项之和为扫描候选数）。
        """
        zone_name = _normalize_zone(zone)
        effective_today = payload.today or datetime.now(timezone.utc).date()
        nodes = [(node.name, node.serial) for node in payload.nodes]
        try:
            return build_release_plan(zone_name, nodes, effective_today)
        except ReleasePlanError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": exc.code,
                    "message": str(exc),
                    "date": exc.date_str,
                    "candidate_window": (
                        {"start": exc.window[0], "end": exc.window[1]}
                        if exc.window is not None
                        else None
                    ),
                    "checked": exc.checked,
                    "blockers": exc.blockers,
                },
            ) from exc

    return app


app = create_app()
