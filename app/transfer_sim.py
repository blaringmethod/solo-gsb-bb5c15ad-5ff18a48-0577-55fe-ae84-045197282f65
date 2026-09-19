"""区域传送收敛预演（不落库）。

第 0 轮：源节点切换到发布 serial（请求校验已保证其相对源节点当前值为
RFC 1982 有效前进）。此后按同步轮次演化：每个节点从**上一轮**各上游
持有的 serial 中，选出相对自身当前值为有效前进（advance）且前向距离
最小者（距离并列时按上游节点名升序取先），所有节点同时切换；没有
有效前进上游的节点保持原值。如此往复，直至某轮不再有任何变更（收敛）
或同步轮次达到 max_rounds。环路中的节点（含源节点）沿用同一规则。

节点只会切换到上游已持有的值，系统中出现过的 serial 集合不增，且每次
切换都是严格前进，因此轮次足够时必然收敛；达到上限后仍有节点可更新时
抛出 :class:`TransferSimulationError`（由 API 层转换为 HTTP 409，并
携带当前快照）。
"""
from __future__ import annotations

from .rfc1982 import SERIAL_HALF, forward_distance

# TransferSimulationError.code 取值（用于 409 响应中的 reason 字段）
ERR_NOT_CONVERGED = "not_converged"  # 达到 max_rounds 后仍有节点可更新


class TransferSimulationError(Exception):
    """达到轮次上限仍未收敛（对应 HTTP 409 Conflict）。

    :param code: 机器可读的原因代码；
    :param rounds: 截至上限的逐轮变更；
    :param snapshot: 达到上限时的当前快照（与请求 nodes 顺序一致）；
    :param unconverged: 仍可更新的节点名；
    :param max_rounds: 本次预演的同步轮次上限。
    """

    def __init__(
        self,
        message: str,
        code: str,
        *,
        rounds: list[dict],
        snapshot: list[dict],
        unconverged: list[str],
        max_rounds: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.rounds = rounds
        self.snapshot = snapshot
        self.unconverged = unconverged
        self.max_rounds = max_rounds


def _next_changes(
    order: list[str], values: dict[str, int], upstreams: dict[str, set[str]]
) -> list[dict]:
    """基于当前快照计算下一轮的同步变更（只读 values，不施加变更）。"""
    changes: list[dict] = []
    for name in order:
        current = values[name]
        best: tuple[int, str, int] | None = None  # (前向距离, 上游名, 新值)
        for upstream in sorted(upstreams[name]):
            candidate = values[upstream]
            distance = forward_distance(current, candidate)
            if 1 <= distance < SERIAL_HALF:  # RFC 1982 有效前进
                if best is None or (distance, upstream) < (best[0], best[1]):
                    best = (distance, upstream, candidate)
        if best is not None:
            distance, upstream, candidate = best
            changes.append(
                {
                    "node": name,
                    "from_serial": current,
                    "to_serial": candidate,
                    "upstream": upstream,
                    "forward_distance": distance,
                }
            )
    return changes


def simulate_transfer(
    zone: str,
    nodes: list[tuple[str, int]],
    edges: list[tuple[str, str]],
    source: str,
    publish_serial: int,
    max_rounds: int,
) -> dict:
    """运行区域传送收敛预演，返回结果字典；未收敛时抛 TransferSimulationError。

    :param nodes: ``(节点名, 当前 serial)`` 列表（顺序即快照输出顺序）；
    :param edges: ``(上游, 下游)`` 有向传送边列表；
    :param source: 源节点名，第 0 轮切换到 ``publish_serial``；
    :param max_rounds: 同步更新轮次上限（不含第 0 轮源节点切换）。
    """
    order = [name for name, _ in nodes]
    values = dict(nodes)
    upstreams: dict[str, set[str]] = {name: set() for name in order}
    for upstream, downstream in edges:
        upstreams[downstream].add(upstream)

    # 第 0 轮：源节点切换到发布值
    rounds: list[dict] = [
        {
            "round": 0,
            "changes": [
                {
                    "node": source,
                    "from_serial": values[source],
                    "to_serial": publish_serial,
                    "upstream": None,
                    "forward_distance": forward_distance(values[source], publish_serial),
                }
            ],
        }
    ]
    values[source] = publish_serial

    converged = False
    for round_no in range(1, max_rounds + 1):
        changes = _next_changes(order, values, upstreams)
        if not changes:
            converged = True
            break
        # 同步应用：所有变更都已基于上一轮快照计算完毕
        for change in changes:
            values[change["node"]] = change["to_serial"]
        rounds.append({"round": round_no, "changes": changes})

    unconverged: list[str] = []
    if not converged:
        # 达到上限：检查是否仍有节点可更新（最后一轮恰好收敛则不算未收敛）
        unconverged = [
            change["node"] for change in _next_changes(order, values, upstreams)
        ]
        converged = not unconverged

    snapshot = [{"name": name, "serial": values[name]} for name in order]
    if not converged:
        raise TransferSimulationError(
            f"达到同步轮次上限 {max_rounds} 后仍有 {len(unconverged)} 个节点可更新，"
            "区域传送未收敛",
            ERR_NOT_CONVERGED,
            rounds=rounds,
            snapshot=snapshot,
            unconverged=unconverged,
            max_rounds=max_rounds,
        )
    return {
        "zone": zone,
        "source": source,
        "publish_serial": publish_serial,
        "max_rounds": max_rounds,
        "converged": True,
        "rounds": rounds,
        "snapshot": snapshot,
        "unconverged": [],
        "note": "传送收敛预演仅用于发布前推演，不写入数据库",
    }
