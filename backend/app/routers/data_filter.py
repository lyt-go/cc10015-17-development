"""数据筛选接口：维护筛选规则，并生成可复现、可交接的筛选结果数据集。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    ConflictPayload,
    GeneratePayload,
    GenerateResult,
    PageResult,
    RevisePayload,
)
from app.services.data_filter import DataFilterService, FilterDatasetService

router = APIRouter(prefix="/api/data_filter", tags=["数据筛选"])

service = DataFilterService()
dataset_service = FilterDatasetService()

LIST_FIELDS = ["规则编号", "数据来源", "筛选字段", "筛选算子", "筛选阈值", "逻辑组合", "输出集合", "规则版本"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤数据筛选列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="data_filter.csv"'})


@router.get("/archives")
def list_archives(rule_code: str | None = Query(default=None, description="按规则编号检索留档记录")) -> dict:
    """留档核对记录：事务残留回滚与数据集保留旧的核对结论。"""
    items = dataset_service.list_archives(rule_code=rule_code)
    return {"items": items, "total": len(items)}


@router.get("/datasets", response_model=PageResult[dict])
def list_datasets(
    rule_code: str | None = Query(default=None, description="按规则编号过滤结果集合"),
    status: str | None = Query(default=None, description="生成中、待确认、已确认、历史"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
) -> PageResult[dict]:
    """筛选结果数据集列表；与详情接口读取同一组数据。"""
    items, total = dataset_service.list_datasets(rule_code=rule_code, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/datasets/{dataset_id}", response_model=dict)
def get_dataset(dataset_id: int) -> dict:
    """单个筛选结果数据集详情，含命中记录；与列表侧栏同一组数据。"""
    dataset = dataset_service.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"筛选结果数据集 {dataset_id} 不存在或已归档")
    return dataset


@router.post("/datasets/{dataset_id}/resume", response_model=ActionResult)
def resume_dataset(dataset_id: int) -> ActionResult:
    """筛选中断后按缺失记录续写；补全前集合不可导出。"""
    dataset, message = dataset_service.resume_dataset(dataset_id)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.post("/datasets/{dataset_id}/confirm", response_model=ActionResult)
def confirm_dataset(dataset_id: int) -> ActionResult:
    """确认结果：确认后消费端才能取得全量筛选结果数据集。"""
    dataset, message = dataset_service.confirm_dataset(dataset_id)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.get("/datasets/{dataset_id}/download")
def download_dataset(dataset_id: int) -> StreamingResponse:
    """下载筛选结果数据集 CSV；同一规则版本重复下载得到完全相同的文件。"""
    dataset = dataset_service.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"筛选结果数据集 {dataset_id} 不存在或已归档")
    text, error = dataset_service.dataset_csv(dataset_id)
    if text is None:
        raise HTTPException(status_code=409, detail=error)
    return StreamingResponse(
        iter([text]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dataset["集合编号"]}.csv"'})


@router.get("/datasets/{dataset_id}/package")
def package_dataset(dataset_id: int) -> StreamingResponse:
    """打包交接：结果确认后向消费端交付 CSV 与 manifest 的压缩包。"""
    dataset = dataset_service.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"筛选结果数据集 {dataset_id} 不存在或已归档")
    content, error = dataset_service.dataset_package(dataset_id)
    if content is None:
        raise HTTPException(status_code=409, detail=error)
    return StreamingResponse(
        iter([content]), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{dataset["集合编号"]}.zip"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条筛选规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"筛选规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条筛选规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="筛选规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条筛选规则执行提交校验、标记生效、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/generate", response_model=GenerateResult)
def generate_dataset(entry_id: int, payload: GeneratePayload) -> GenerateResult:
    """按当前规则版本生成筛选结果数据集；同一规则版本重复生成复用稳定输出集合。"""
    dataset, message, extra = dataset_service.generate_dataset(
        entry_id, simulate_interrupt=payload.simulate_interrupt)
    return GenerateResult(
        ok=dataset is not None, message=message, dataset=dataset,
        conflict=extra.get("conflict"))


@router.post("/{entry_id}/revise", response_model=ActionResult)
def revise_rule(entry_id: int, payload: RevisePayload) -> ActionResult:
    """规则修订：递增规则版本，之后生成会触发版本冲突判定。"""
    entry, message = service.revise_rule(entry_id, payload.new_version)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/resolve_conflict", response_model=GenerateResult)
def resolve_conflict(entry_id: int, payload: ConflictPayload) -> GenerateResult:
    """判定版本冲突：固定旧规则版本生成历史集合，或采用新规则版本生成当前集合。"""
    dataset, message, extra = dataset_service.resolve_conflict(
        entry_id, payload.strategy.strip(), simulate_interrupt=payload.simulate_interrupt)
    return GenerateResult(
        ok=dataset is not None, message=message, dataset=dataset,
        conflict=extra.get("conflict"))


@router.post("/{entry_id}/reconcile", response_model=ActionResult)
def reconcile(entry_id: int) -> ActionResult:
    """留档核对：数据集保留旧时事务一旦残留便回滚，并按规则版本留档。"""
    archive, message = dataset_service.reconcile(entry_id)
    if archive is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=archive)
