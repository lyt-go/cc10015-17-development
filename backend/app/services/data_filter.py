"""数据筛选业务规则：状态流转、字段校验与筛选口径都收在这里。

筛选结果数据集（filter_dataset）是本模块的稳定输出集合：

- 生成时把规则版本与结果集合同事务写入，重复生成同一规则版本会得到完全相同的文件；
- 中断的集合只能按缺失记录续写，补全之前不允许标记为可导出；
- 数据集保留旧版本时一旦出现事务残留必须回滚，并按规则版本留档核对。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import time
import zipfile
from typing import Any

from app.store import store

MODULE = "data_filter"
DATASET_MODULE = "filter_dataset"
ARCHIVE_MODULE = "filter_dataset_archive"
LIST_FIELDS = ["规则编号", "数据来源", "筛选字段", "筛选算子", "筛选阈值", "逻辑组合", "输出集合", "规则版本"]
EXTRA_FIELDS = ["指标编号", "统计粒度"]
REQUIRED_FIELDS = ["规则编号", "数据来源", "筛选字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]

DATASET_STATUSES = ["生成中", "待确认", "已确认", "历史"]
SOURCE_RECORD_COUNT = 24
CONFLICT_STRATEGIES = ["pin_old", "use_new"]

_dataset_lock = threading.Lock()


def _parse_list(text: Any) -> list[str]:
    """把逗号（含中文逗号）分隔的页面输入拆成干净的条件列表。"""
    return [part.strip() for part in str(text or "").replace("，", ",").split(",") if part.strip()]


class DataFilterService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        for field in EXTRA_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"筛选规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据筛选可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"筛选规则已{action}"

    def revise_rule(self, entry_id: int, new_version: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """规则修订：递增规则版本（如 V017 -> V018），之后生成会触发版本冲突判定。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"筛选规则 {entry_id} 不存在或已归档"
        if not new_version:
            digits = "".join(ch for ch in str(entry.get("规则版本") or "") if ch.isdigit())
            new_version = f"V{int(digits or 0) + 1:03d}"
        entry["规则版本"] = str(new_version).strip()
        store.bump_version(MODULE)
        return entry, f"规则 {entry.get('规则编号')} 已修订为 {entry['规则版本']}"


class FilterDatasetService:
    """筛选结果数据集：生成、续写、确认、留档核对与文件交接。"""

    # ---------- 读取：列表侧栏与详情页共用同一组数据 ----------

    def list_datasets(
        self,
        *,
        rule_code: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(DATASET_MODULE)
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self._summary(row) for row in rows[start:start + size]], total

    def get_dataset(self, dataset_id: int) -> dict[str, Any] | None:
        return store.find(DATASET_MODULE, dataset_id)

    def list_archives(self, *, rule_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ARCHIVE_MODULE)
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        return [dict(row) for row in rows]

    # ---------- 生成：规则版本与结果集合同事务写入 ----------

    def generate_dataset(
        self, rule_id: int, *, simulate_interrupt: bool = False
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"筛选规则 {rule_id} 不存在或已归档", {}
        version = str(rule.get("规则版本") or "").strip()
        current = self._current_dataset(str(rule.get("规则编号")))
        if current is not None:
            if current.get("tx_pending"):
                return None, f"集合 {current['集合编号']} 存在事务残留，请先留档核对回滚", {
                    "residue": current["集合编号"]}
            if current["状态"] == "生成中":
                return None, f"集合 {current['集合编号']} 尚未生成完整，请续写缺失记录", {
                    "resumable": current["集合编号"]}
            if current["规则版本"] == version:
                return current, f"规则版本 {version} 已生成，复用稳定输出集合 {current['集合编号']}", {}
            return None, (
                f"规则版本已修订为 {version}，与当前集合 {current['集合编号']}"
                f"（{current['规则版本']}）冲突，需判定固定旧版本还是采用新版本"
            ), {"conflict": {"旧版本": current["规则版本"], "新版本": version, "当前集合": current["集合编号"]}}
        dataset = self._write_dataset(rule, self._snapshot_of(rule), simulate_interrupt=simulate_interrupt)
        if dataset["状态"] == "生成中":
            return dataset, (
                f"筛选中断，集合 {dataset['集合编号']} 已写入 {dataset['记录数']} 条，"
                "需按缺失记录续写后才可导出"
            ), {}
        return dataset, f"筛选结果数据集 {dataset['集合编号']} 已生成，规则版本 {version} 与结果集合已同时写入", {}

    # ---------- 续写与确认 ----------

    def resume_dataset(self, dataset_id: int) -> tuple[dict[str, Any] | None, str]:
        dataset = store.find(DATASET_MODULE, dataset_id)
        if dataset is None:
            return None, f"筛选结果数据集 {dataset_id} 不存在或已归档"
        if dataset.get("tx_pending"):
            return None, f"集合 {dataset['集合编号']} 存在事务残留，请先留档核对回滚"
        if dataset["状态"] != "生成中":
            return None, f"集合 {dataset['集合编号']} 状态为{dataset['状态']}，无需续写"
        full = self._evaluate(dataset["rule_snapshot"])
        existing = {rec["记录编号"] for rec in dataset["records"]}
        missing = [rec for rec in full if rec["记录编号"] not in existing]
        dataset["records"].extend(missing)
        dataset["记录数"] = len(dataset["records"])
        dataset["content_hash"] = self._content_hash(dataset["规则版本"], dataset["records"])
        dataset["状态"] = "待确认"
        dataset["exportable"] = True
        return dataset, f"已按缺失记录续写 {len(missing)} 条，集合 {dataset['集合编号']} 完整可导出"

    def confirm_dataset(self, dataset_id: int) -> tuple[dict[str, Any] | None, str]:
        dataset = store.find(DATASET_MODULE, dataset_id)
        if dataset is None:
            return None, f"筛选结果数据集 {dataset_id} 不存在或已归档"
        if not dataset.get("exportable"):
            return None, f"集合 {dataset['集合编号']} 尚未生成完整，半套集合不能标记为可导出"
        if dataset["状态"] != "待确认":
            return None, f"集合 {dataset['集合编号']} 状态为{dataset['状态']}，仅待确认集合可确认"
        dataset["状态"] = "已确认"
        return dataset, f"集合 {dataset['集合编号']} 结果已确认，消费端可取得全量筛选结果数据集"

    # ---------- 留档核对：数据集保留旧时事务残留便回滚 ----------

    def reconcile(self, rule_id: int) -> tuple[dict[str, Any] | None, str]:
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"筛选规则 {rule_id} 不存在或已归档"
        rule_code = str(rule.get("规则编号"))
        residue = [row for row in self._datasets_of(rule_code) if row.get("tx_pending")]
        rolled_back: list[str] = []
        restored: list[str] = []
        with _dataset_lock:
            for dead in residue:
                mark = dead["集合编号"]
                store.rows(DATASET_MODULE).remove(dead)
                rolled_back.append(mark)
                for old in self._datasets_of(rule_code):
                    if old.get("tx_id") == mark:
                        old["状态"] = old.get("之前状态", "待确认")
                        old.pop("tx_id", None)
                        old.pop("之前状态", None)
                        restored.append(old["集合编号"])
                        rule["输出集合"] = old["集合编号"]
                        rule["当前集合版本"] = old["规则版本"]
            if residue:
                conclusion = f"事务残留已回滚：{'、'.join(rolled_back)}；旧集合保留：{'、'.join(restored)}"
            else:
                conclusion = "无事务残留，数据集保留旧环节核对通过"
            new_id = store.next_id(ARCHIVE_MODULE)
            archive = {
                "id": new_id,
                "归档编号": f"ARCH-{new_id:04d}",
                "规则编号": rule_code,
                "核对版本": str(rule.get("规则版本") or ""),
                "回滚集合": rolled_back,
                "恢复集合": restored,
                "核对结论": conclusion,
                "核对时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            store.rows(ARCHIVE_MODULE).append(archive)
        return archive, f"已按 {archive['核对版本']} 完成留档核对：{conclusion}"

    # ---------- 版本冲突：固定旧版本生成历史集合，或采用新版本生成当前集合 ----------

    def resolve_conflict(
        self, rule_id: int, strategy: str, *, simulate_interrupt: bool = False
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"筛选规则 {rule_id} 不存在或已归档", {}
        version = str(rule.get("规则版本") or "").strip()
        current = self._current_dataset(str(rule.get("规则编号")))
        if current is None or current["规则版本"] == version:
            return None, "当前无版本冲突，可直接生成筛选结果数据集", {}
        if strategy == "pin_old":
            snapshot = dict(current["rule_snapshot"])
            dataset = self._write_dataset(rule, snapshot, history=True)
            dataset["新规则差异"] = self._rule_diff(snapshot, rule)
            return dataset, (
                f"已固定旧规则版本 {snapshot.get('规则版本')} 生成历史集合 {dataset['集合编号']}，"
                "旧规则结果保留并附新规则差异"
            ), {}
        if strategy == "use_new":
            dataset = self._write_dataset(
                rule, self._snapshot_of(rule), simulate_interrupt=simulate_interrupt)
            if dataset.get("tx_pending"):
                return dataset, f"版本切换中断，集合 {dataset['集合编号']} 存在事务残留，请留档核对回滚", {}
            if dataset["状态"] == "生成中":
                return dataset, f"筛选中断，集合 {dataset['集合编号']} 需按缺失记录续写后才可导出", {}
            return dataset, f"已采用新规则版本 {version} 生成当前集合 {dataset['集合编号']}，旧版本列为历史", {}
        return None, f"未知策略「{strategy}」，可选：固定旧版本 pin_old / 采用新版本 use_new", {}

    # ---------- 文件交接：下载与打包 ----------

    def dataset_csv(self, dataset_id: int) -> tuple[str | None, str]:
        dataset = store.find(DATASET_MODULE, dataset_id)
        if dataset is None:
            return None, f"筛选结果数据集 {dataset_id} 不存在或已归档"
        if not dataset.get("exportable"):
            return None, f"集合 {dataset['集合编号']} 尚未生成完整，半套集合不能标记为可导出"
        return self._render_csv(dataset), ""

    def dataset_package(self, dataset_id: int) -> tuple[bytes | None, str]:
        dataset = store.find(DATASET_MODULE, dataset_id)
        if dataset is None:
            return None, f"筛选结果数据集 {dataset_id} 不存在或已归档"
        if dataset["状态"] != "已确认":
            return None, f"集合 {dataset['集合编号']} 结果确认后才能打包交接消费端"
        manifest = {
            "集合编号": dataset["集合编号"],
            "规则编号": dataset["规则编号"],
            "规则版本": dataset["规则版本"],
            "指标编号": dataset["指标编号"],
            "统计粒度": dataset["统计粒度"],
            "命中条件": dataset["命中条件"],
            "记录数": dataset["记录数"],
            "content_hash": dataset["content_hash"],
            "指标校验": dataset["指标校验"],
            "交接说明": "筛选结果数据集打包交接，文件内容按规则版本可复现",
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in (
                (f"{dataset['集合编号']}.csv", self._render_csv(dataset)),
                ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)),
            ):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, content)
        return buffer.getvalue(), ""

    # ---------- 内部：确定性生成，保证同一规则版本可复现 ----------

    def _datasets_of(self, rule_code: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(DATASET_MODULE) if row.get("规则编号") == rule_code]

    def _current_dataset(self, rule_code: str) -> dict[str, Any] | None:
        sets = [row for row in self._datasets_of(rule_code) if row["状态"] != "历史"]
        return sets[-1] if sets else None

    def _summary(self, dataset: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in dataset.items() if key not in ("records", "rule_snapshot")}

    def _snapshot_of(self, rule: dict[str, Any]) -> dict[str, Any]:
        snapshot = {field: rule.get(field) for field in LIST_FIELDS}
        for field in EXTRA_FIELDS:
            snapshot[field] = rule.get(field)
        return snapshot

    def _conditions(self, snapshot: dict[str, Any]) -> list[dict[str, str]]:
        fields = _parse_list(snapshot.get("筛选字段"))
        operators = _parse_list(snapshot.get("筛选算子"))
        thresholds = _parse_list(snapshot.get("筛选阈值"))
        conditions = []
        for index, field in enumerate(fields):
            operator = operators[index] if index < len(operators) else ">="
            threshold = thresholds[index] if index < len(thresholds) else "0"
            conditions.append({
                "字段": field, "算子": operator, "阈值": threshold,
                "描述": f"{field} {operator} {threshold}",
            })
        return conditions

    def _source_records(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """由数据来源与规则编号确定性生成源记录，同一规则版本永远得到同一批输入。"""
        conditions = self._conditions(snapshot)
        fields = [cond["字段"] for cond in conditions] or ["转化率"]
        seed_base = f"{snapshot.get('数据来源', '')}|{snapshot.get('规则编号', '')}"
        records = []
        for index in range(SOURCE_RECORD_COUNT):
            digest = hashlib.sha256(f"{seed_base}|{index}".encode("utf-8")).hexdigest()
            values = {
                field: round(int(digest[pos * 8:(pos + 1) * 8], 16) / 0xFFFFFFFF, 4)
                for pos, field in enumerate(fields)
            }
            records.append({
                "记录编号": f"REC-{snapshot.get('规则编号', '')}-{index + 1:04d}",
                "指标编号": snapshot.get("指标编号", ""),
                "统计粒度": snapshot.get("统计粒度", ""),
                "日期分区": f"2026-09-{index % 28 + 1:02d}",
                "字段值": values,
            })
        return records

    def _apply_op(self, value: float, operator: str, threshold: str) -> bool:
        try:
            limit = float(threshold)
        except (TypeError, ValueError):
            return False
        if operator == ">=":
            return value >= limit
        if operator == "<=":
            return value <= limit
        if operator == ">":
            return value > limit
        if operator == "<":
            return value < limit
        if operator == "==":
            return value == limit
        if operator == "!=":
            return value != limit
        return value >= limit

    def _evaluate(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """按逻辑组合把筛选条件应用到稳定源记录上，命中记录带命中条件。"""
        conditions = self._conditions(snapshot)
        require_all = str(snapshot.get("逻辑组合") or "并且") != "或者"
        hits = []
        for record in self._source_records(snapshot):
            matched = [
                cond["描述"] for cond in conditions
                if self._apply_op(record["字段值"].get(cond["字段"], 0.0), cond["算子"], cond["阈值"])
            ]
            hit = len(matched) == len(conditions) if require_all else bool(matched)
            if conditions and hit:
                row = dict(record)
                row["命中条件"] = matched
                hits.append(row)
        return hits

    def _content_hash(self, version: Any, records: list[dict[str, Any]]) -> str:
        payload = json.dumps({"规则版本": version, "records": records}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _metric_validation(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """跨模块联动：按指标编号读取指标定义，校验筛选字段与统计粒度。"""
        code = str(snapshot.get("指标编号") or "").strip()
        if not code:
            return {"通过": False, "说明": "规则未配置指标编号，无法联动指标定义校验"}
        metric = store.find_by_field("metric_def", "指标编号", code)
        if metric is None:
            return {"通过": False, "说明": f"指标定义中不存在指标编号 {code}"}
        problems = []
        if str(snapshot.get("统计粒度") or "") != str(metric.get("统计粒度") or ""):
            problems.append(f"统计粒度不一致：筛选={snapshot.get('统计粒度') or '空'}，指标={metric.get('统计粒度')}")
        if not _parse_list(snapshot.get("筛选字段")):
            problems.append("筛选字段为空")
        if problems:
            return {"通过": False, "说明": "；".join(problems), "指标名称": metric.get("指标名称")}
        return {"通过": True, "说明": f"指标 {code} 筛选字段与统计粒度校验通过", "指标名称": metric.get("指标名称")}

    def _rule_diff(self, old_snapshot: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        diff: dict[str, Any] = {}
        for field in [*LIST_FIELDS, *EXTRA_FIELDS]:
            old, new = old_snapshot.get(field), rule.get(field)
            if str(old or "") != str(new or ""):
                diff[field] = {"旧": old, "新": new}
        return diff

    def _write_dataset(
        self,
        rule: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        simulate_interrupt: bool = False,
        history: bool = False,
    ) -> dict[str, Any]:
        """规则版本与结果集合同事务写入；任一步失败都会回滚两侧。"""
        records = self._evaluate(snapshot)
        interrupted = simulate_interrupt and len(records) > 1
        written = records[: max(1, len(records) // 2)] if interrupted else records
        new_id = store.next_id(DATASET_MODULE)
        dataset = {
            "id": new_id,
            "集合编号": f"DSET-{new_id:04d}",
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "指标编号": snapshot.get("指标编号"),
            "统计粒度": snapshot.get("统计粒度"),
            "命中条件": [cond["描述"] for cond in self._conditions(snapshot)],
            "记录数": len(written),
            "状态": "历史" if history else ("生成中" if interrupted else "待确认"),
            "exportable": not interrupted,
            "tx_pending": False,
            "content_hash": self._content_hash(snapshot.get("规则版本"), written),
            "records": written,
            "rule_snapshot": snapshot,
            "指标校验": self._metric_validation(snapshot),
            "新规则差异": None,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        prev_output = rule.get("输出集合")
        prev_tag = rule.get("当前集合版本")
        with _dataset_lock:
            marked: list[dict[str, Any]] = []
            try:
                if not history:
                    for old in self._datasets_of(str(rule.get("规则编号"))):
                        if old["状态"] != "历史":
                            old["之前状态"] = old["状态"]
                            old["状态"] = "历史"
                            old["tx_id"] = dataset["集合编号"]
                            marked.append(old)
                store.rows(DATASET_MODULE).append(dataset)
                if not history:
                    rule["输出集合"] = dataset["集合编号"]
                    rule["当前集合版本"] = dataset["规则版本"]
                if interrupted and marked:
                    # 版本切换中途断裂：旧集合已翻历史、新集合未写完，属于事务残留
                    dataset["tx_pending"] = True
                else:
                    for old in marked:
                        old.pop("tx_id", None)
                        old.pop("之前状态", None)
            except Exception:
                if dataset in store.rows(DATASET_MODULE):
                    store.rows(DATASET_MODULE).remove(dataset)
                for old in marked:
                    old["状态"] = old.pop("之前状态")
                    old.pop("tx_id", None)
                rule["输出集合"] = prev_output
                rule["当前集合版本"] = prev_tag
                raise
        return dataset

    def _render_csv(self, dataset: dict[str, Any]) -> str:
        """确定性渲染：同一规则版本的集合导出完全相同的文件内容。"""
        fields = [cond["字段"] for cond in self._conditions(dataset["rule_snapshot"])]
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["集合编号", dataset["集合编号"], "规则编号", dataset["规则编号"],
                         "规则版本", dataset["规则版本"]])
        writer.writerow(["记录编号", "指标编号", "统计粒度", "日期分区", "命中条件", *fields])
        for record in dataset["records"]:
            writer.writerow([
                record["记录编号"], record["指标编号"], record["统计粒度"], record["日期分区"],
                "；".join(record["命中条件"]),
                *(record["字段值"].get(field, "") for field in fields),
            ])
        return output.getvalue()
