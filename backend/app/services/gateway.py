"""接入网关管理业务规则：状态流转、字段校验与筛选口径都收在这里。

批量切换监听端口 / 并发上限采用「分批计划 + 顺序生效 + 单批回退」的方式：

* 每个网关配置连同它在服务注册发现里的依赖实例拆成一个批次，先落库计划再执行；
* 每批保存变更前的网关运行状态、目标状态、执行序号与已完成位置；
* 端口校验放在进程内锁里顺序做，同一个监听端口只有首个通过校验的批次生效；
* 已成功批次登记签名，后续重试只跳过、不重复排空连接，也不覆盖成功结果；
* 失败批次只回退自己的连接，旧监听端口连接保留；成功批次不受影响；
* 每批结果（成功或回退）同步到查询路由分发中的目标端口。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.services.router import RouterService
from app.store import store

MODULE = "gateway"
REQUIRED_FIELDS = ["配置编号", "网关名称", "监听端口"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已关闭"]
ACTION_RULES = {"启动网关": "运行中", "暂停网关": "已暂停", "关闭网关": "已关闭"}
NEGATIVE_ACTIONS = []

BATCH_APPLIED = "已生效"
BATCH_SKIPPED = "已跳过"
BATCH_ROLLED_BACK = "已回退"
BATCH_PENDING = "待生效"

# UI 里可多选的候选监听端口；避开示例数据里的当前端口，方便直接试切。
PORT_CANDIDATES = [19081, 19082, 19083, 19084, 19085]
DEFAULT_DRAIN_SECONDS = 30

# 同一进程里并发提交时，只有先拿到锁并先通过端口校验的批次能生效。
_SWITCH_LOCK = threading.RLock()

router_service = RouterService()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class GatewayService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        port = _to_int(values.get("监听端口"))
        entry["监听端口"] = port if port is not None else values.get("监听端口")
        entry["上一版监听端口"] = entry["监听端口"]
        entry["并发上限"] = _to_int(values.get("并发上限")) or 100
        entry["上一版并发上限"] = entry["并发上限"]
        entry["上一版运行状态"] = STATUS_ORDER[0]
        entry["完成位置"] = "未执行"
        entry["最近批次编号"] = None
        entry["关联实例"] = values.get("关联实例") or []
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"网关配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于接入网关管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"网关配置已{action}"

    # ------------------------------------------------------------------
    # 分批切换
    # ------------------------------------------------------------------
    def port_options(self) -> dict[str, Any]:
        """返回可多选的候选监听端口及其实时占用情况。"""
        rows = store.rows(MODULE)
        running = [row for row in rows if row.get("status") == "运行中"]
        used: dict[int, str] = {}
        for row in running:
            port = _to_int(row.get("监听端口"))
            if port is not None:
                used.setdefault(port, str(row.get("配置编号")))
        ports = [
            {"端口": port, "可用": port not in used, "占用网关": used.get(port)}
            for port in PORT_CANDIDATES
        ]
        gateways = [
            {
                "id": row.get("id"),
                "配置编号": row.get("配置编号"),
                "网关名称": row.get("网关名称"),
                "当前监听端口": row.get("监听端口"),
                "当前并发上限": row.get("并发上限"),
                "上一版监听端口": row.get("上一版监听端口"),
                "完成位置": row.get("完成位置"),
                "运行状态": row.get("status"),
                "依赖实例": row.get("关联实例") or [],
            }
            for row in rows
        ]
        return {"ports": ports, "gateways": gateways}

    def list_batches(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        plans = store.meta_list("gateway_batch_plans")
        ordered = list(reversed(plans))
        total = len(ordered)
        start = max(page - 1, 0) * size
        return ordered[start:start + size], total

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for plan in store.meta_list("gateway_batch_plans"):
            if plan.get("batch_id") == batch_id:
                return plan
        return None

    def list_connections(
        self,
        *,
        batch_id: str | None = None,
        gateway_id: int | None = None,
    ) -> list[dict[str, Any]]:
        connections = store.meta_list("gateway_connections")
        if batch_id:
            connections = [row for row in connections if row.get("批次编号") == batch_id]
        if gateway_id is not None:
            connections = [row for row in connections if int(row.get("网关ID", 0)) == gateway_id]
        return connections

    def switch_batches(
        self,
        batches: list[dict[str, Any]],
        *,
        drain_seconds: int = DEFAULT_DRAIN_SECONDS,
    ) -> dict[str, Any]:
        """提交多个网关批次：先保存每批计划，再按顺序校验、生效或回退。"""
        drain_seconds = max(0, min(_to_int(drain_seconds) or 0, 600))
        normalized = [self._normalize_item(item, index) for index, item in enumerate(batches, start=1)]
        if not normalized:
            return {
                "ok": False,
                "message": "没有可提交的网关批次",
                "results": [],
                "totals": {"已生效": 0, "已跳过": 0, "已回退": 0},
            }

        with _SWITCH_LOCK:
            plans = store.meta_list("gateway_batch_plans")
            total = len(normalized)
            current_plans: list[dict[str, Any]] = []
            seen_configs: set[str] = set()

            for index, item in enumerate(normalized, start=1):
                batch_id = self._next_batch_id()
                plan = self._build_plan(batch_id, index, total, item, drain_seconds, seen_configs)
                plans.append(plan)
                current_plans.append(plan)

            for plan in current_plans:
                if plan["状态"] == BATCH_PENDING:
                    self._execute_plan(plan, drain_seconds)

            results = [self._public_result(plan) for plan in current_plans]
            totals = {
                BATCH_APPLIED: sum(1 for plan in current_plans if plan["状态"] == BATCH_APPLIED),
                BATCH_SKIPPED: sum(1 for plan in current_plans if plan["状态"] == BATCH_SKIPPED),
                BATCH_ROLLED_BACK: sum(1 for plan in current_plans if plan["状态"] == BATCH_ROLLED_BACK),
            }
            ok = totals[BATCH_ROLLED_BACK] == 0
            message = (
                f"共提交 {total} 批：已生效 {totals[BATCH_APPLIED]} 批，"
                f"已跳过 {totals[BATCH_SKIPPED]} 批，已回退 {totals[BATCH_ROLLED_BACK]} 批"
            )
            return {
                "ok": ok,
                "message": message,
                "drain_seconds": drain_seconds,
                "results": results,
                "totals": totals,
            }

    # ------------------------------------------------------------------
    # 计划构建与执行
    # ------------------------------------------------------------------
    def _normalize_item(self, item: dict[str, Any], index: int) -> dict[str, Any]:
        config_no = str(
            item.get("配置编号") or item.get("config_no") or item.get("gateway_config") or ""
        ).strip()
        gateway_id = _to_int(item.get("网关ID") or item.get("gateway_id") or item.get("id"))
        return {
            "序号": index,
            "配置编号": config_no,
            "网关ID": gateway_id,
            "监听端口": item.get("监听端口", item.get("port")),
            "并发上限": item.get("并发上限", item.get("concurrency")),
        }

    def _next_batch_id(self) -> str:
        seq = int(store.meta("gateway_batch_seq", 0)) + 1
        store.set_meta("gateway_batch_seq", seq)
        return f"GB-{datetime.now().strftime('%Y%m%d')}-{seq:04d}"

    def _resolve_gateway(self, item: dict[str, Any]) -> dict[str, Any] | None:
        config_no = item.get("配置编号")
        if config_no:
            for row in store.rows(MODULE):
                if str(row.get("配置编号")) == config_no:
                    return row
            return None
        gateway_id = item.get("网关ID")
        if gateway_id is not None:
            return store.find(MODULE, gateway_id)
        return None

    def _build_plan(
        self,
        batch_id: str,
        index: int,
        total: int,
        item: dict[str, Any],
        drain_seconds: int,
        seen_configs: set[str],
    ) -> dict[str, Any]:
        gateway = self._resolve_gateway(item)
        target_port = _to_int(item.get("监听端口"))
        target_concurrency = _to_int(item.get("并发上限"))
        snapshot = self._gateway_snapshot(gateway)
        dependencies = list((gateway or {}).get("关联实例") or [])
        plan: dict[str, Any] = {
            "batch_id": batch_id,
            "执行序号": index,
            "批次总数": total,
            "配置编号": (gateway or {}).get("配置编号") or item.get("配置编号") or "",
            "网关ID": (gateway or {}).get("id"),
            "网关名称": (gateway or {}).get("网关名称"),
            "依赖实例": dependencies,
            "依赖快照": self._dependency_snapshot(dependencies),
            "变更前": snapshot,
            "变更后": {
                "监听端口": target_port,
                "并发上限": target_concurrency if target_concurrency is not None else snapshot.get("并发上限"),
            },
            "状态": BATCH_PENDING,
            "消息": "",
            "已完成位置": None,
            "申请时间": _now(),
            "完成时间": None,
            "排空策略": f"优雅排空 {drain_seconds} 秒：新端口连接先建立，旧端口连接进入排空期后关闭",
            "连接": {"旧端口连接": [], "新端口连接": [], "保留数量": 0, "关闭数量": 0},
            "路由同步": {"状态": "未同步", "规则": [], "说明": ""},
        }

        failure = self._plan_validation_failure(item, gateway, target_port, seen_configs)
        if failure:
            plan["状态"] = BATCH_ROLLED_BACK
            plan["消息"] = failure
            plan["完成时间"] = _now()
        return plan

    def _plan_validation_failure(
        self,
        item: dict[str, Any],
        gateway: dict[str, Any] | None,
        target_port: int | None,
        seen_configs: set[str],
    ) -> str | None:
        if gateway is None:
            return "网关配置不存在或已归档，本批未执行"
        config_no = str(gateway.get("配置编号"))
        if config_no in seen_configs:
            return f"网关配置 {config_no} 在一次提交里出现多次，后提交批次未执行"
        seen_configs.add(config_no)
        if target_port is None:
            return "监听端口必须是 1-65535 之间的整数，本批未执行"
        if not 1 <= target_port <= 65535:
            return f"监听端口 {target_port} 超出 1-65535，本批未执行"
        target_concurrency = _to_int(item.get("并发上限"))
        if item.get("并发上限") not in (None, "") and (target_concurrency is None or target_concurrency <= 0):
            return "并发上限必须是正整数，本批未执行"
        if gateway.get("status") != "运行中":
            return f"网关当前状态为「{gateway.get('status')}」，仅运行中网关参与批量切换"
        return None

    def _execute_plan(self, plan: dict[str, Any], drain_seconds: int) -> None:
        gateway = store.find(MODULE, int(plan.get("网关ID") or 0))
        if gateway is None:
            self._mark_rollback(plan, None, "网关配置不存在或已归档")
            return

        target_port = plan["变更后"]["监听端口"]
        target_concurrency = plan["变更后"]["并发上限"]
        old_port = _to_int(plan["变更前"].get("监听端口"))
        old_concurrency = _to_int(plan["变更前"].get("并发上限"))

        # 先做端口占用预检：失败时不会建立任何新连接，旧端口连接原样保留。
        failure = self._port_conflict_failure(gateway, target_port)
        if failure:
            self._mark_rollback(plan, gateway, failure)
            return

        # 目标值与当前运行值一致：属于未受影响批次，直接跳过，保证成功批次不被重试覆盖。
        if (
            target_port == old_port
            and target_concurrency == old_concurrency
        ):
            plan["状态"] = BATCH_SKIPPED
            plan["消息"] = (
                f"网关已在监听端口 {target_port}、并发上限 {target_concurrency} 运行，"
                "保留既有成功批次，不重复切换"
            )
            plan["完成位置"] = gateway.get("完成位置")
            plan["完成时间"] = _now()
            # 成功批次的路由同步结果保持原批次记录，重试不重新写同步时间。
            plan["路由同步"] = {
                "状态": "保持已同步",
                "规则": [],
                "说明": f"网关已在上一次成功批次 {gateway.get('最近批次编号')} 完成切换，本次不重复同步",
            }
            return

        port_changed = target_port != old_port
        if port_changed:
            # 优雅排空：先建立新端口连接并让旧端口连接进入排空期，再复核依赖健康；
            # 复核失败时只关闭本批新端口连接，旧端口连接原样保留。
            old_connections = self._ensure_gateway_connections(gateway, old_port)
            for connection in old_connections:
                connection["状态"] = "排空中"
                connection["排空截止"] = (
                    datetime.now() + timedelta(seconds=drain_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
            new_connections = self._create_connections(gateway, target_port, plan["batch_id"])
            plan["连接"]["旧端口连接"] = old_connections
            plan["连接"]["新端口连接"] = new_connections
            plan["连接"]["保留数量"] = len(old_connections)

            failure = self._dependency_failure(gateway)
            if failure:
                self._mark_rollback(plan, gateway, failure)
                return
        else:
            failure = self._dependency_failure(gateway)
            if failure:
                self._mark_rollback(plan, gateway, failure)
                return
            # 只调整并发上限时不动连接，避免无谓排空。
            for connection in self._ensure_gateway_connections(gateway, old_port):
                connection["状态"] = "使用中"

        gateway["上一版监听端口"] = old_port
        gateway["上一版并发上限"] = old_concurrency
        gateway["上一版运行状态"] = plan["变更前"].get("运行状态")
        gateway["监听端口"] = target_port
        gateway["并发上限"] = target_concurrency
        gateway["完成位置"] = f"第{plan['执行序号']}/{plan['批次总数']}批"
        gateway["最近批次编号"] = plan["batch_id"]
        gateway["pending"] = False

        plan["状态"] = BATCH_APPLIED
        plan["已完成位置"] = plan["执行序号"]
        plan["完成时间"] = _now()
        plan["消息"] = (
            f"监听端口 {old_port} → {target_port} 已生效，"
            f"旧端口连接按 {drain_seconds} 秒排空"
            if port_changed
            else f"监听端口保持 {old_port}，并发上限 {old_concurrency} → {target_concurrency} 已生效"
        )
        plan["路由同步"] = self._sync_router(gateway, old_port, target_port, plan["batch_id"], True)

    def _port_conflict_failure(self, gateway: dict[str, Any], target_port: int) -> str | None:
        for row in store.rows(MODULE):
            if row.get("id") == gateway.get("id"):
                continue
            if row.get("status") == "运行中" and _to_int(row.get("监听端口")) == target_port:
                return (
                    f"监听端口 {target_port} 已被运行中网关 {row.get('配置编号')} 占用，"
                    "同一端口仅首个通过校验的批次生效"
                )
        return None

    def _dependency_failure(self, gateway: dict[str, Any]) -> str | None:
        for instance_no in gateway.get("关联实例") or []:
            instance = next(
                (
                    row
                    for row in store.rows("registry")
                    if str(row.get("实例编号")) == str(instance_no)
                ),
                None,
            )
            if instance is None:
                return f"依赖实例 {instance_no} 不存在，批次未生效"
            if instance.get("status") != "已注册" or instance.get("健康状态") != "健康":
                return (
                    f"依赖实例 {instance_no} 状态为「{instance.get('status')}」、"
                    f"健康状态为「{instance.get('健康状态')}」，批次未生效"
                )
        return None

    def _mark_rollback(self, plan: dict[str, Any], gateway: dict[str, Any] | None, reason: str) -> None:
        """只回退当前批次：旧端口连接保留，新端口连接关闭，网关运行状态恢复快照。"""
        old_port = _to_int(plan["变更前"].get("监听端口"))
        new_port = _to_int(plan["变更后"].get("监听端口"))
        kept: list[dict[str, Any]] = []
        closed: list[dict[str, Any]] = []

        if gateway is not None:
            kept = self._ensure_gateway_connections(gateway, old_port)
            for connection in kept:
                connection["状态"] = "使用中"
                connection["备注"] = "回退保留旧端口连接"
            closed = [
                connection
                for connection in store.meta_list("gateway_connections")
                if connection.get("批次编号") == plan["batch_id"]
                and connection.get("端口") == new_port
            ]
            for connection in closed:
                connection["状态"] = "已关闭"
                connection["备注"] = "批次回退，新端口连接已关闭"
            # 恢复变更前的运行状态快照，成功批次的其他网关不受影响。
            snapshot = plan["变更前"]
            if snapshot:
                for field in (
                    "监听端口",
                    "并发上限",
                    "上一版监听端口",
                    "上一版并发上限",
                    "上一版运行状态",
                    "完成位置",
                    "最近批次编号",
                ):
                    if field in snapshot:
                        gateway[field] = snapshot[field]
            plan["路由同步"] = self._sync_router(gateway, old_port, old_port, plan["batch_id"], False)

        plan["连接"]["旧端口连接"] = kept
        plan["连接"]["新端口连接"] = closed
        plan["连接"]["保留数量"] = len(kept)
        plan["连接"]["关闭数量"] = len(closed)
        plan["状态"] = BATCH_ROLLED_BACK
        plan["已完成位置"] = None
        plan["完成时间"] = _now()
        plan["消息"] = (
            f"{reason}；已回退第 {plan['执行序号']} 批并保留旧监听端口 {old_port} 的连接，"
            "其他批次不受影响"
        )

    def _gateway_snapshot(self, gateway: dict[str, Any] | None) -> dict[str, Any]:
        if gateway is None:
            return {}
        return {
            "监听端口": gateway.get("监听端口"),
            "并发上限": gateway.get("并发上限"),
            "运行状态": gateway.get("status"),
            "上一版监听端口": gateway.get("上一版监听端口"),
            "上一版并发上限": gateway.get("上一版并发上限"),
            "上一版运行状态": gateway.get("上一版运行状态"),
            "完成位置": gateway.get("完成位置"),
            "最近批次编号": gateway.get("最近批次编号"),
        }

    def _dependency_snapshot(self, dependencies: list[str]) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for instance_no in dependencies:
            instance = next(
                (
                    row
                    for row in store.rows("registry")
                    if str(row.get("实例编号")) == str(instance_no)
                ),
                None,
            )
            if instance is None:
                snapshot.append({"实例编号": instance_no, "状态": "不存在", "健康状态": None})
            else:
                snapshot.append(
                    {
                        "实例编号": instance_no,
                        "服务名称": instance.get("服务名称"),
                        "状态": instance.get("status"),
                        "健康状态": instance.get("健康状态"),
                    }
                )
        return snapshot

    # ------------------------------------------------------------------
    # 连接与路由
    # ------------------------------------------------------------------
    def _ensure_gateway_connections(
        self, gateway: dict[str, Any], port: int | None
    ) -> list[dict[str, Any]]:
        """确保网关在旧端口上至少有一组可保留的连接记录。"""
        connections = store.meta_list("gateway_connections")
        gateway_id = int(gateway.get("id", 0))
        existing = [
            connection
            for connection in connections
            if int(connection.get("网关ID", 0)) == gateway_id
            and connection.get("端口") == port
            and connection.get("状态") in ("使用中", "排空中")
        ]
        if existing:
            return existing

        configured = gateway.get("配置编号")
        instance_ids = list(gateway.get("关联实例") or []) or ["-"]
        created: list[dict[str, Any]] = []
        for instance_no in instance_ids:
            connection = {
                "连接编号": self._next_connection_id(gateway_id),
                "网关ID": gateway_id,
                "配置编号": configured,
                "实例编号": instance_no,
                "端口": port,
                "状态": "使用中",
                "创建时间": _now(),
                "批次编号": "-",
                "备注": "旧监听端口连接",
            }
            connections.append(connection)
            created.append(connection)
        return created

    def _create_connections(
        self, gateway: dict[str, Any], port: int, batch_id: str
    ) -> list[dict[str, Any]]:
        connections = store.meta_list("gateway_connections")
        gateway_id = int(gateway.get("id", 0))
        instance_ids = list(gateway.get("关联实例") or []) or ["-"]
        created: list[dict[str, Any]] = []
        for instance_no in instance_ids:
            connection = {
                "连接编号": self._next_connection_id(gateway_id),
                "网关ID": gateway_id,
                "配置编号": gateway.get("配置编号"),
                "实例编号": instance_no,
                "端口": port,
                "状态": "使用中",
                "创建时间": _now(),
                "批次编号": batch_id,
                "备注": "新监听端口连接",
            }
            connections.append(connection)
            created.append(connection)
        return created

    def _next_connection_id(self, gateway_id: int) -> str:
        existing = [
            row
            for row in store.meta_list("gateway_connections")
            if int(row.get("网关ID", 0)) == gateway_id
        ]
        return f"CN-{gateway_id}-{len(existing) + 1:04d}"

    def _sync_router(
        self,
        gateway: dict[str, Any],
        old_port: int | None,
        new_port: int | None,
        batch_id: str,
        succeeded: bool,
    ) -> dict[str, Any]:
        gateway_name = str(gateway.get("网关名称") or "")
        rules = router_service.sync_gateway_switch(
            gateway_name=gateway_name,
            old_port=old_port,
            new_port=new_port,
            batch_id=batch_id,
            succeeded=succeeded,
        )
        return {
            "状态": "已同步" if succeeded else "已回退同步",
            "规则": rules,
            "说明": (
                f"{batch_id} 生效，目标端口更新为 {new_port}"
                if succeeded
                else f"{batch_id} 回退，路由保留旧端口 {old_port}"
            ),
        }

    def _public_result(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": plan["batch_id"],
            "执行序号": plan["执行序号"],
            "批次总数": plan["批次总数"],
            "配置编号": plan["配置编号"],
            "网关名称": plan["网关名称"],
            "状态": plan["状态"],
            "消息": plan["消息"],
            "依赖实例": plan["依赖实例"],
            "变更前监听端口": plan["变更前"].get("监听端口"),
            "目标监听端口": plan["变更后"].get("监听端口"),
            "变更前并发上限": plan["变更前"].get("并发上限"),
            "目标并发上限": plan["变更后"].get("并发上限"),
            "上一版监听端口": plan["变更前"].get("上一版监听端口"),
            "已完成位置": plan["已完成位置"],
            "完成位置": store.find(MODULE, int(plan.get("网关ID") or 0)).get("完成位置")
            if plan.get("网关ID")
            else None,
            "旧端口连接保留": plan["连接"].get("保留数量", 0),
            "新端口连接关闭": plan["连接"].get("关闭数量", 0),
            "路由同步": plan["路由同步"],
        }
