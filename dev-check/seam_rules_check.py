"""纯标准库复刻 fieldwork.py 的接缝厚差规则，端到端验证业务场景。

不依赖 SQLAlchemy：用字典模拟表，逻辑与 backend/domain/fieldwork.py 保持同构。
两个场区隔离验证，避免跨场景时序耦合。
"""
from statistics import median

DEPTH_MIN, DEPTH_MAX, SPREAD_LIMIT = 12, 16, 2
MIN_NEIGHBORS, LIMIT = 2, 1.0

cells_tbl = {}          # cell_id -> {id, zone, ref, opened, snapshot_pred, snapshot_dev}
readings_tbl = []       # {id, cell_id, kind, depths, passed, by, repeat_of}
reviews_tbl = []        # {id, cell_id, state, trigger, prediction, deviation, ...}
_next = {"cell": 0, "reading": 0, "review": 0}


def new_id(k):
    _next[k] += 1
    return _next[k]


def neighbors(ref):
    row, col = ref[0], int(ref[1:])
    out = []
    if row > "A":
        out.append(chr(ord(row) - 1) + str(col))
    if row < "D":
        out.append(chr(ord(row) + 1) + str(col))
    if col > 1:
        out.append(row + str(col - 1))
    if col < 8:
        out.append(row + str(col + 1))
    return out


def depths_pass(depths):
    return all(DEPTH_MIN <= d <= DEPTH_MAX for d in depths) and max(depths) - min(depths) <= SPREAD_LIMIT


def mean(depths):
    return sum(depths) / 3


def latest_routine(cell_id):
    latest = None
    for r in readings_tbl:
        if r["cell_id"] == cell_id and r["kind"] == "routine":
            latest = r
    return latest


def zone_cells(zone):
    return {c["ref"]: c for c in cells_tbl.values() if c["zone"] == zone}


def prediction(zone, ref):
    zc = zone_cells(zone)
    ids = [zc[n]["id"] for n in neighbors(ref) if n in zc]
    means = []
    for cid in ids:
        r = latest_routine(cid)
        if r and r["passed"]:
            means.append(mean(r["depths"]))
    if len(means) < MIN_NEIGHBORS:
        return None
    return median(sorted(means))


def open_review(cell_id):
    return next((v for v in reviews_tbl if v["cell_id"] == cell_id and v["state"] == "open"), None)


def recompute(zone, skip_cell_id=None):
    """邻格常规读数更新：整区其余未解决复核单作废后重算（与 _recompute_zone_seam 同构）。"""
    active_ids = [
        c["id"] for c in cells_tbl.values()
        if c["zone"] == zone and not c["opened"] and c["id"] != skip_cell_id
    ]
    for v in reviews_tbl:
        if v["cell_id"] in active_ids and v["state"] == "open":
            v["state"] = "voided"
            v["reason"] = "neighbor_reading_updated"
    for ref, cell in zone_cells(zone).items():
        if cell["opened"] or cell["id"] == skip_cell_id:
            continue
        r = latest_routine(cell["id"])
        if not r or not r["passed"]:
            continue
        pred = prediction(zone, ref)
        if pred is None:
            continue
        dev = abs(mean(r["depths"]) - pred)
        if dev > LIMIT:
            reviews_tbl.append({"id": new_id("review"), "cell_id": cell["id"], "state": "open",
                                "trigger": r["id"], "prediction": pred, "deviation": dev,
                                "created_by": r["by"]})


def evaluate_self(zone, cell_id, reading):
    """本格按最新常规读数单独判定接缝厚差（与 _evaluate_cell_seam 同构）。"""
    if not reading or not reading["passed"]:
        return
    pred = prediction(zone, cells_tbl[cell_id]["ref"])
    if pred is None:
        return
    dev = abs(mean(reading["depths"]) - pred)
    if dev > LIMIT:
        reviews_tbl.append({"id": new_id("review"), "cell_id": cell_id, "state": "open",
                            "trigger": reading["id"], "prediction": pred, "deviation": dev,
                            "created_by": reading["by"]})


def add_cell(zone, ref, opened=False):
    cid = new_id("cell")
    cells_tbl[cid] = {"id": cid, "zone": zone, "ref": ref, "opened": opened,
                      "snapshot_pred": None, "snapshot_dev": None}
    return cid


def measure(zone, cid, depths, by):
    prev = latest_routine(cid)
    if prev and prev["passed"]:
        raise AssertionError("合格后不能再提常规读数")
    rid = new_id("reading")
    reading = {"id": rid, "cell_id": cid, "kind": "routine", "depths": depths,
               "passed": depths_pass(depths), "by": by,
               "repeat_of": prev["id"] if prev else None}
    readings_tbl.append(reading)
    for v in reviews_tbl:  # 本格重测：自身旧复核单作废
        if v["cell_id"] == cid and v["state"] == "open":
            v["state"] = "voided"
            v["reason"] = "cell_reread"
    evaluate_self(zone, cid, reading)
    recompute(zone, skip_cell_id=cid)
    return rid


def seam_review(zone, cid, depths, by):
    v = open_review(cid)
    assert v, "没有待处理复核单"
    trigger = next(r for r in readings_tbl if r["id"] == v["trigger"])
    assert trigger["by"] != by, "必须换人复核"
    rid = new_id("reading")
    ok = depths_pass(depths)
    readings_tbl.append({"id": rid, "cell_id": cid, "kind": "seam_review", "depths": depths,
                         "passed": ok, "by": by, "repeat_of": trigger["id"]})
    if ok:
        v["state"] = "resolved"
        v["review_reading"] = rid
    return ok


def qualified(cid):
    r = latest_routine(cid)
    return bool(r and r["passed"] and open_review(cid) is None)


def open_patch(cell_ids, by):
    assert all(qualified(cid) for cid in cell_ids), "开场时仍有不合格或待复核格位"
    for cid in cell_ids:
        cell = cells_tbl[cid]
        cell["opened"] = True
        r = latest_routine(cid)
        cell["snapshot_pred"] = prediction(cell["zone"], cell["ref"])
        cell["snapshot_dev"] = (
            abs(mean(r["depths"]) - cell["snapshot_pred"])
            if cell["snapshot_pred"] is not None else None
        )


results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# ===== 场区一：触发、换人复核、不入预测、快照 =====
Z1 = "YH-NORTH"
b1 = add_cell(Z1, "B1")
a2 = add_cell(Z1, "A2")
b2 = add_cell(Z1, "B2")

check("初始无预测", prediction(Z1, "B2") is None)
measure(Z1, b1, (12, 12, 12), "operator@demo.test")
check("仅一个合格邻格仍无预测", prediction(Z1, "B2") is None)
measure(Z1, a2, (14, 14, 14), "operator@demo.test")
check("两邻格预测=中位数13", prediction(Z1, "B2") == 13)

measure(Z1, b2, (15, 15, 15), "operator@demo.test")  # 均值15 偏离2
v = open_review(b2)
check("合格但偏离>1mm触发复核", v is not None and abs(v["deviation"] - 2.0) < 1e-9)
check("待复核格不算可开场", not qualified(b2))

try:
    seam_review(Z1, b2, (13, 13, 13), "operator@demo.test")
    check("原测量人不能自复核", False)
except AssertionError:
    check("原测量人不能自复核", True)

ok = seam_review(Z1, b2, (12, 14, 15), "reviewer@demo.test")  # 极差3，不过原标准
check("复核未过原标准返回失败", ok is False)
check("复核失败后复核单仍待处理", open_review(b2) is not None)
check("复核读数不入预测（预测仍13）", prediction(Z1, "B2") == 13)

ok = seam_review(Z1, b2, (13, 13, 13), "reviewer@demo.test")
check("换人复核通过后可开场", ok and open_review(b2) is None and qualified(b2))
check("复核通过读数仍不入预测", prediction(Z1, "B2") == 13)

# 偏离恰为 1mm 不触发；无读数格不可开场；开场快照
b3 = add_cell(Z1, "B3")
check("无读数格不可开场", not qualified(b3))
a1 = add_cell(Z1, "A1")  # 邻 A2(均值14)、B1(均值12) -> 预测13
measure(Z1, a1, (14, 14, 14), "operator@demo.test")  # 均值14，偏离恰为1
check("偏离恰为1mm不触发复核", open_review(a1) is None and qualified(a1))
measure(Z1, b3, (13, 13, 13), "operator@demo.test")  # 仅 B2 一个合格邻格 -> 无预测
check("单合格邻格的 B3 不触发复核", open_review(b3) is None and qualified(b3))
# 整区重算的连带结果：B1（邻 A1=14、B2=15，预测14.5，偏离2.5）与 B2 被重开复核，逐一换人解决
for cid in (b1, b2):
    review = open_review(cid)
    check(f"{cells_tbl[cid]['ref']} 经整区重算挂复核", review is not None)
    seam_review(Z1, cid, (14, 14, 14), "reviewer@demo.test")
open_patch([b1, a2, b2, a1, b3], "reviewer@demo.test")
check("开场留住 B2 预测快照", cells_tbl[b2]["snapshot_pred"] == 13.0)
check("开场快照记录 B2 偏离2", abs(cells_tbl[b2]["snapshot_dev"] - 2.0) < 1e-9)

# ===== 场区二：邻格更新、作废/重开、整区重算、已开场冻结 =====
Z2 = "YH-EAST"
# 本格 C3，邻格 C2、D3（B3/C4 不铺）
c3 = add_cell(Z2, "C3")
c2 = add_cell(Z2, "C2")
d3 = add_cell(Z2, "D3")

measure(Z2, d3, (11, 11, 11), "operator@demo.test")  # 失败：不作预测来源
measure(Z2, c2, (14, 13, 14), "operator@demo.test")  # 均值 13.667 合格
measure(Z2, c3, (16, 16, 16), "operator@demo.test")  # 仅一个合格邻格 -> 无预测
check("单合格邻格时不触发复核", open_review(c3) is None and qualified(c3))

# 边界格 D1 无任何邻格
d1 = add_cell(Z2, "D1")
measure(Z2, d1, (16, 16, 16), "operator@demo.test")
check("无合格邻格时不触发复核", open_review(d1) is None and qualified(d1))

# D3 复测合格（均值16）：预测=中位数(13.667,16)=14.833，C3 偏离 1.167 >1 -> 触发
measure(Z2, d3, (16, 16, 16), "operator@demo.test")
oldv = open_review(c3)
expected_pred = (13 + 2 / 3 + 16) / 2
check("邻格更新后超差新开复核单",
      oldv is not None and abs(oldv["prediction"] - expected_pred) < 1e-9
      and abs(oldv["deviation"] - abs(16 - expected_pred)) < 1e-9)

# 由另一个邻格 C2 复测驱动整区重算（先让它失败才能再测）：旧单作废，仍超差则重开
# C2 当前已合格无法重测，改为新增邻格 B3 后补首条读数，同样触发整区重算
b3z2 = add_cell(Z2, "B3")
measure(Z2, b3z2, (12, 12, 12), "operator@demo.test")  # 均值12，成为 C3 第三个合格邻格
check("邻格更新后旧单先作废", oldv["state"] == "voided")
newv = open_review(c3)
# 新预测=中位数(12,13.667,16)=13.667，C3 均值16 偏离2.333 仍超1 -> 重开
check("重算后仍超差则重开复核单", newv is not None and newv["id"] != oldv["id"])

# 合格格拒绝重复常规读数
try:
    measure(Z2, d3, (12, 12, 12), "operator@demo.test")
    check("合格格拒绝重复常规读数", False)
except AssertionError:
    check("合格格拒绝重复常规读数", True)

# 复核解决后开场；已开场格不参与重算，但读数仍作邻格预测来源
seam_review(Z2, c3, (14, 15, 14), "reviewer@demo.test")
check("复核解决后可开场", qualified(c3))
open_patch([c2, d3, c3, d1], "reviewer@demo.test")
# 新格 C1 邻 C2(已开场,均值13.667)、B1(无)、D1(已开场,均值16) -> 两来源
c1 = add_cell(Z2, "C1")
measure(Z2, c1, (12, 12, 12), "operator@demo.test")
check("已开场邻格仍作预测来源", prediction(Z2, "C1") == expected_pred)
# 已开场格上不应再有 open 复核单（重算跳过）
check("已开场格不参与重算", open_review(c3) is None and open_review(d1) is None)

# 三邻格中位数：D5 邻 C5、D4、D6
d5 = add_cell(Z2, "D5")
d4 = add_cell(Z2, "D4")
d6 = add_cell(Z2, "D6")
c5 = add_cell(Z2, "C5")
measure(Z2, d4, (12, 12, 12), "operator@demo.test")
measure(Z2, d6, (14, 14, 14), "operator@demo.test")
measure(Z2, c5, (16, 16, 16), "operator@demo.test")
measure(Z2, d5, (14, 14, 14), "operator@demo.test")  # 预测中位数(12,14,16)=14，偏离0
check("三邻格中位数预测=14", prediction(Z2, "D5") == 14 and qualified(d5))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
