"""筛选结果数据集链路测试：稳定输出、断点续写、事务回滚与跨模块联动。"""
import io
import json
import sys, os
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.data_filter import DataFilterService, FilterDatasetService
from app.services.metric_def import MetricDefService


def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []


def _make_rule(**overrides):
    values = {
        "规则编号": "DATA-1001",
        "数据来源": "实验事件明细",
        "筛选字段": "转化率",
        "筛选算子": ">=",
        "筛选阈值": "0.5",
        "逻辑组合": "并且",
        "输出集合": "待生成",
        "规则版本": "V017",
        "指标编号": "METR-1001",
        "统计粒度": "日",
    }
    values.update(overrides)
    entry, missing = DataFilterService().create_entry(values)
    assert entry is not None, missing
    return entry


def _make_metric(**overrides):
    values = {
        "指标编号": "METR-1001",
        "指标名称": "转化率",
        "计算公式": "order_cnt / visit_cnt",
        "统计粒度": "日",
        "时间窗口": "7",
        "基准取值": "0.5",
        "精度位数": "4",
        "口径版本": "v1",
    }
    values.update(overrides)
    entry, missing = MetricDefService().create_entry(values)
    assert entry is not None, missing
    return entry


def test_same_version_reproduces_identical_dataset():
    """重复生成同一规则版本得到完全相同的筛选结果文件；版本变化生成新的集合。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    first, msg, _ = svc.generate_dataset(rule["id"])
    assert first is not None, msg
    again, msg, _ = svc.generate_dataset(rule["id"])
    assert again["集合编号"] == first["集合编号"], "同一规则版本应复用稳定输出集合"
    csv1, err1 = svc.dataset_csv(first["id"])
    csv2, err2 = svc.dataset_csv(again["id"])
    assert err1 == "" and err2 == ""
    assert csv1 == csv2, "同一规则版本的筛选结果文件应完全相同"
    # 版本变化 → 生成新的集合，旧版本列为历史
    DataFilterService().revise_rule(rule["id"])
    blocked, msg, extra = svc.generate_dataset(rule["id"])
    assert blocked is None and "conflict" in extra
    newer, msg, _ = svc.resolve_conflict(rule["id"], "use_new")
    assert newer["集合编号"] != first["集合编号"]
    assert newer["规则版本"] == "V018"
    assert newer["content_hash"] != first["content_hash"]
    assert svc.get_dataset(first["id"])["状态"] == "历史"


def test_rule_version_and_dataset_written_atomically():
    """规则版本与结果集合同时写入，重新生成可确认使用了哪套阈值。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    dataset, msg, _ = svc.generate_dataset(rule["id"])
    assert dataset is not None, msg
    assert rule["输出集合"] == dataset["集合编号"]
    assert rule["当前集合版本"] == "V017"
    snapshot = dataset["rule_snapshot"]
    assert snapshot["规则版本"] == "V017"
    assert snapshot["筛选阈值"] == rule["筛选阈值"]
    assert dataset["命中条件"] == ["转化率 >= 0.5"]
    assert all(rec["命中条件"] for rec in dataset["records"])


def test_interrupted_generation_resumes_by_missing_records():
    """筛选中断后按缺失记录续写；半套集合不能标记为可导出。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    dataset, msg, _ = svc.generate_dataset(rule["id"], simulate_interrupt=True)
    assert dataset["状态"] == "生成中"
    assert dataset["exportable"] is False
    full = svc._evaluate(dataset["rule_snapshot"])
    assert 0 < dataset["记录数"] < len(full)
    # 半套集合不可导出、不可确认
    text, error = svc.dataset_csv(dataset["id"])
    assert text is None and "半套" in error
    denied, msg = svc.confirm_dataset(dataset["id"])
    assert denied is None and "半套" in msg
    # 按缺失记录续写后集合完整，内容与一次性生成一致
    done, msg = svc.resume_dataset(dataset["id"])
    assert done["状态"] == "待确认"
    assert done["exportable"] is True
    assert done["记录数"] == len(full)
    assert done["content_hash"] == svc._content_hash("V017", full)
    text, error = svc.dataset_csv(dataset["id"])
    assert text and error == ""


def test_retain_old_transaction_residue_rolls_back():
    """数据集保留旧时事务一旦残留便回滚，并按规则版本留档核对。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    old_set, _, _ = svc.generate_dataset(rule["id"])
    svc.confirm_dataset(old_set["id"])
    DataFilterService().revise_rule(rule["id"])  # V017 -> V018
    broken, msg, _ = svc.resolve_conflict(rule["id"], "use_new", simulate_interrupt=True)
    assert broken["tx_pending"] is True
    assert svc.get_dataset(old_set["id"])["状态"] == "历史"
    # 残留集合不可续写、不可重复生成
    resumed, msg = svc.resume_dataset(broken["id"])
    assert resumed is None and "回滚" in msg
    blocked, msg, extra = svc.generate_dataset(rule["id"])
    assert blocked is None and extra.get("residue") == broken["集合编号"]
    # 留档核对：回滚残留、恢复旧集合、写入留档记录
    archive, msg = svc.reconcile(rule["id"])
    assert archive["核对版本"] == "V018"
    assert broken["集合编号"] in archive["回滚集合"]
    assert old_set["集合编号"] in archive["恢复集合"]
    assert svc.get_dataset(broken["id"]) is None
    restored = svc.get_dataset(old_set["id"])
    assert restored["状态"] == "已确认"
    assert rule["输出集合"] == old_set["集合编号"]
    assert rule["当前集合版本"] == "V017"
    archives = svc.list_archives(rule_code=rule["规则编号"])
    assert len(archives) == 1
    # 无残留时核对直接通过
    again, msg = svc.reconcile(rule["id"])
    assert "无事务残留" in again["核对结论"]


def test_pin_old_generates_historical_set_with_rule_diff():
    """固定旧规则版本生成历史集合：保留旧规则结果并附新规则差异。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    old_set, _, _ = svc.generate_dataset(rule["id"])
    DataFilterService().revise_rule(rule["id"])
    hist, msg, _ = svc.resolve_conflict(rule["id"], "pin_old")
    assert hist["状态"] == "历史"
    assert hist["规则版本"] == "V017"
    assert hist["content_hash"] == old_set["content_hash"], "旧规则结果应完全保留"
    assert hist["新规则差异"]["规则版本"] == {"旧": "V017", "新": "V018"}
    assert rule["输出集合"] == old_set["集合编号"], "历史集合不改变当前输出指向"
    # 同一冲突再判定为采用新版本：保存新规则结果并把旧版本列为历史
    new_set, msg, _ = svc.resolve_conflict(rule["id"], "use_new")
    assert new_set["规则版本"] == "V018"
    assert svc.get_dataset(old_set["id"])["状态"] == "历史"
    assert rule["输出集合"] == new_set["集合编号"]
    assert rule["当前集合版本"] == "V018"


def test_metric_def_reads_and_validates_filter_datasets():
    """跨模块联动：指标定义读取筛选结果数据集中的指标编号，校验筛选字段与统计粒度。"""
    _clear_all()
    rule = _make_rule()
    metric = _make_metric()
    svc = FilterDatasetService()
    dataset, msg, _ = svc.generate_dataset(rule["id"])
    assert dataset["指标校验"]["通过"] is True
    report, error = MetricDefService().filter_dataset_validations(metric["id"])
    assert report["total"] == 1
    item = report["items"][0]
    assert item["集合编号"] == dataset["集合编号"]
    assert item["规则版本"] == "V017"
    assert item["通过"] is True
    # 统计粒度不一致时校验不通过
    rule2 = _make_rule(规则编号="DATA-1002", 统计粒度="周")
    bad, msg, _ = svc.generate_dataset(rule2["id"])
    assert bad["指标校验"]["通过"] is False
    assert "统计粒度" in bad["指标校验"]["说明"]
    report2, _ = MetricDefService().filter_dataset_validations(metric["id"])
    assert report2["total"] == 2
    assert any(not row["通过"] for row in report2["items"])


def test_package_handoff_requires_confirmation_and_is_deterministic():
    """结果确认后交消费端取得全量筛选结果数据集；打包内容可复现。"""
    _clear_all()
    rule = _make_rule()
    _make_metric()
    svc = FilterDatasetService()
    dataset, _, _ = svc.generate_dataset(rule["id"])
    content, error = svc.dataset_package(dataset["id"])
    assert content is None and "确认" in error
    svc.confirm_dataset(dataset["id"])
    pkg1, err1 = svc.dataset_package(dataset["id"])
    pkg2, err2 = svc.dataset_package(dataset["id"])
    assert err1 == "" and err2 == ""
    assert pkg1 == pkg2, "同一规则版本的打包文件应完全相同"
    with zipfile.ZipFile(io.BytesIO(pkg1)) as zf:
        assert sorted(zf.namelist()) == [f"{dataset['集合编号']}.csv", "manifest.json"]
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["规则版本"] == "V017"
    assert manifest["记录数"] == dataset["记录数"]
    assert manifest["指标校验"]["通过"] is True
    assert manifest["命中条件"] == ["转化率 >= 0.5"]
