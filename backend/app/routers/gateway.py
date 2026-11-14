"""接入网关管理接口：维护网关配置，覆盖启动网关、暂停网关、关闭网关等动作。

批量切换监听端口 / 并发上限不直接改写全部网关，而是先提交分批计划，再由服务层
按批次校验、生效或回退，并逐条返回每批结果。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.gateway import DEFAULT_DRAIN_SECONDS, GatewayService

router = APIRouter(prefix="/api/gateway", tags=["接入网关管理"])

service = GatewayService()

LIST_FIELDS = ["配置编号", "网关名称", "监听端口", "上一版监听端口", "协议类型", "认证方式", "超时秒数", "并发上限", "完成位置", "配置状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已关闭"]


class GatewaySwitchBatch(BaseModel):
    """一个网关批次：一条网关配置 + 它在服务注册发现中的依赖实例。"""

    model_config = ConfigDict(populate_by_name=True)

    config_no: str | None = Field(default=None, alias="配置编号")
    gateway_id: int | None = Field(default=None, alias="网关ID")
    port: int | None = Field(default=None, alias="监听端口")
    concurrency: int | None = Field(default=None, alias="并发上限")


class GatewayBatchSwitchPayload(BaseModel):
    """一次提交多个网关批次，可多选监听端口，逐条返回每批生效状态。"""

    batches: list[GatewaySwitchBatch] = Field(default_factory=list)
    drain_seconds: int = Field(default=DEFAULT_DRAIN_SECONDS, ge=0, le=600)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤接入网关管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出接入网关管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "gateway", "total": total, "items": items}


@router.get("/ports")
def list_ports() -> dict[str, Any]:
    """候选监听端口（支持多选）与当前占用情况，附网关当前端口和依赖实例。"""
    return service.port_options()


@router.get("/batches", response_model=PageResult[dict])
def list_batches(page: int = 1, size: int = 20) -> PageResult[dict]:
    """分批切换计划列表：包含每批状态、完成位置、变更前后端口与排空记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: str) -> dict:
    """读取单个批次的接入计划与执行结果。"""
    plan = service.get_batch(batch_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return plan


@router.post("/batches", response_model=dict)
def switch_batches(payload: GatewayBatchSwitchPayload) -> dict:
    """一次提交多个网关批次：按顺序生效，失败批次单独回退，逐批返回状态。"""
    items = [batch.model_dump(by_alias=True) for batch in payload.batches]
    return service.switch_batches(items, drain_seconds=payload.drain_seconds)


@router.get("/connections")
def list_connections(
    batch_id: str | None = Query(default=None, description="按批次编号过滤"),
    gateway_id: int | None = Query(default=None, description="按网关过滤"),
) -> dict[str, Any]:
    """连接排空记录：失败批次保留旧端口连接，关闭新端口连接。"""
    items = service.list_connections(batch_id=batch_id, gateway_id=gateway_id)
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条网关配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"网关配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条网关配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="网关配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条网关配置执行启动网关、暂停网关、关闭网关；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
