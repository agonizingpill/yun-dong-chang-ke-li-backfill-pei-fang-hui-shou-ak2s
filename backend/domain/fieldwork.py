from datetime import datetime, timezone
from statistics import median

from fastapi import HTTPException
from sqlalchemy import delete, select

from core.audit import record
from core.db import transaction
from domain import catalog
from domain.blend_tables import tickets, zones
from domain.field_tables import cells, holds, patches, readings, seam_reviews

# 接缝厚差规则：邻格预测可用的最少合格邻格数、触发换人复核的偏离阈值（mm）
MIN_SEAM_NEIGHBORS = 2
SEAM_DEVIATION_LIMIT_MM = 1.0
# 原合格标准：三点厚度区间与极差
DEPTH_MIN_MM = 12
DEPTH_MAX_MM = 16
SPREAD_LIMIT_MM = 2


def _neighbors(grid_ref):
    """同区上下左右邻格编号（A1..D8）。"""
    row, column = grid_ref[0], int(grid_ref[1:])
    result = []
    if row > "A":
        result.append(chr(ord(row) - 1) + str(column))
    if row < "D":
        result.append(chr(ord(row) + 1) + str(column))
    if column > 1:
        result.append(row + str(column - 1))
    if column < 8:
        result.append(row + str(column + 1))
    return result


def _reading_mean(reading):
    return (reading["first_mm"] + reading["middle_mm"] + reading["last_mm"]) / 3


def _depths_pass(depths):
    spread = max(depths) - min(depths)
    return all(DEPTH_MIN_MM <= depth <= DEPTH_MAX_MM for depth in depths) and spread <= SPREAD_LIMIT_MM, spread


def _zone_cells(conn, zone_code):
    """区内全部格位（含已开场历史格位），按编号索引。"""
    query = (
        select(cells, patches.c.zone_code, patches.c.opened_by)
        .join(patches, cells.c.patch_code == patches.c.code)
        .where(patches.c.zone_code == zone_code)
    )
    return {row["grid_ref"]: dict(row) for row in conn.execute(query).mappings()}


def _latest_routine_by_cells(conn, cell_ids):
    """每个格位最新一条常规测厚（复核读数永不参与预测）。"""
    if not cell_ids:
        return {}
    query = (
        select(readings)
        .where(readings.c.cell_id.in_(cell_ids), readings.c.kind == "routine")
        .order_by(readings.c.cell_id, readings.c.id)
    )
    latest = {}
    for row in conn.execute(query).mappings():
        latest[row["cell_id"]] = dict(row)
    return latest


def _seam_prediction(conn, zone_code, grid_ref):
    """用合格邻格最新常规读数的三点均值中位数预测本格厚度。

    至少两个合格邻格可用时才返回预测，否则返回 None。
    """
    zone = _zone_cells(conn, zone_code)
    neighbor_ids = [
        zone[ref]["id"] for ref in _neighbors(grid_ref) if ref in zone
    ]
    latest = _latest_routine_by_cells(conn, neighbor_ids)
    means = sorted(
        _reading_mean(reading)
        for reading in latest.values()
        if reading["passed"]
    )
    if len(means) < MIN_SEAM_NEIGHBORS:
        return None
    return median(means)


def _void_open_reviews(conn, zone_cell_ids, reason):
    """邻格读数更新后，区内未解决复核单全部过期作废。"""
    if not zone_cell_ids:
        return
    now = datetime.now(timezone.utc)
    query = seam_reviews.update().where(
        seam_reviews.c.cell_id.in_(zone_cell_ids),
        seam_reviews.c.state == "open",
    ).values(state="voided", voided_reason=reason, resolved_at=now)
    conn.execute(query)


def _recompute_zone_seam(conn, zone_code, void_reason, skip_cell_id=None):
    """邻格常规读数更新后重算整区待开场工单的接缝复核单。

    先把区内所有未解决复核单作废，再依据当前最新常规读数重新判定：
    仍偏离预测超过 1 mm 的格位重新挂复核单，已不满足触发条件的永久消解。
    已开场工单冻结，不参与重算。skip_cell_id 为本次提交读数的格位，
    其复核状态由调用方按新读数单独判定，不在"邻格更新过期"范围内。
    """
    zone = _zone_cells(conn, zone_code)
    active = {
        ref: cell for ref, cell in zone.items()
        if cell["opened_by"] is None and cell["id"] != skip_cell_id
    }
    _void_open_reviews(conn, [cell["id"] for cell in active.values()], void_reason)
    latest = _latest_routine_by_cells(conn, [cell["id"] for cell in active.values()])
    now = datetime.now(timezone.utc)
    for ref, cell in active.items():
        reading = latest.get(cell["id"])
        if reading is None or not reading["passed"]:
            continue
        prediction = _seam_prediction(conn, zone_code, ref)
        if prediction is None:
            continue
        observed = _reading_mean(reading)
        deviation = abs(observed - prediction)
        if deviation > SEAM_DEVIATION_LIMIT_MM:
            conn.execute(seam_reviews.insert().values(
                cell_id=cell["id"], trigger_reading_id=reading["id"], prediction_mm=prediction,
                observed_mean_mm=observed, deviation_mm=deviation, state="open",
                created_by=reading["measured_by"], created_at=now,
            ))


def _evaluate_cell_seam(conn, zone_code, cell, reading):
    """按本格最新常规读数判定自身接缝厚差：合格且偏离预测超过 1 mm 则挂复核单。"""
    if reading is None or not reading["passed"]:
        return
    prediction = _seam_prediction(conn, zone_code, cell["grid_ref"])
    if prediction is None:
        return
    observed = _reading_mean(reading)
    deviation = abs(observed - prediction)
    if deviation > SEAM_DEVIATION_LIMIT_MM:
        conn.execute(seam_reviews.insert().values(
            cell_id=cell["id"], trigger_reading_id=reading["id"], prediction_mm=prediction,
            observed_mean_mm=observed, deviation_mm=deviation, state="open",
            created_by=reading["measured_by"], created_at=datetime.now(timezone.utc),
        ))


def _open_review_map(conn, cell_ids):
    if not cell_ids:
        return {}
    query = (
        select(seam_reviews)
        .where(seam_reviews.c.cell_id.in_(cell_ids), seam_reviews.c.state == "open")
        .order_by(seam_reviews.c.id)
    )
    return {row["cell_id"]: dict(row) for row in conn.execute(query).mappings()}


def _assemble_detail(conn, patch):
    code = patch["code"]
    query = select(cells).where(cells.c.patch_code == code).order_by(cells.c.grid_ref)
    grid = [dict(row) | {"history": []} for row in conn.execute(query).mappings()]
    by_id = {cell["id"]: cell for cell in grid}
    history = select(readings).where(readings.c.cell_id.in_(by_id)).order_by(readings.c.id)
    for row in conn.execute(history).mappings():
        by_id[row["cell_id"]]["history"].append(dict(row))

    open_reviews = _open_review_map(conn, list(by_id))
    for cell in grid:
        routine = next(
            (row for row in reversed(cell["history"]) if row["kind"] == "routine"), None
        )
        cell["latest_routine"] = routine
        review = open_reviews.get(cell["id"])
        if review is not None:
            cell["seam_prediction_mm"] = review["prediction_mm"]
            cell["seam_deviation_mm"] = review["deviation_mm"]
            cell["open_seam_review"] = review
        elif patch["opened_by"] is not None:
            # 已签开场的补坪单留住签字落账时的读数与预测快照
            cell["seam_prediction_mm"] = cell["snapshot_prediction_mm"]
            cell["seam_deviation_mm"] = cell["snapshot_deviation_mm"]
            cell["open_seam_review"] = None
        else:
            prediction = _seam_prediction(conn, patch["zone_code"], cell["grid_ref"])
            if prediction is not None and routine is not None:
                cell["seam_prediction_mm"] = prediction
                cell["seam_deviation_mm"] = abs(_reading_mean(routine) - prediction)
            else:
                cell["seam_prediction_mm"] = None
                cell["seam_deviation_mm"] = None
            cell["open_seam_review"] = None

    passed = sum(
        cell["latest_routine"] is not None
        and cell["latest_routine"]["passed"]
        and cell["open_seam_review"] is None
        for cell in grid
    )
    return {"patch": patch, "cells": grid, "passed_cells": passed}


def board():
    with transaction() as conn:
        # Keep unfinished work visible even when recent opened patches accumulate.
        active = select(patches).where(patches.c.opened_by.is_(None)).order_by(patches.c.code)
        opened = select(patches).where(patches.c.opened_by.is_not(None)).order_by(patches.c.code.desc()).limit(50)
        eligible = (
            select(tickets.c.code, tickets.c.zone_code, tickets.c.total_g)
            .outerjoin(patches, tickets.c.code == patches.c.code)
            .where(tickets.c.state == "released", patches.c.code.is_(None))
            .order_by(tickets.c.code)
        )
        return {
            "patches": [dict(row) for query in (active, opened) for row in conn.execute(query).mappings()],
            "eligible_tickets": [dict(row) for row in conn.execute(eligible).mappings()],
            "active_cells": [dict(row) for row in conn.execute(select(holds)).mappings()],
        }


def details(code):
    with transaction() as conn:
        return _assemble_detail(conn, catalog.lookup(conn, patches, code))


def mark(code, form, user):
    with transaction() as conn:
        ticket = catalog.lookup(conn, tickets, code, lock=True)
        if ticket["state"] != "released":
            raise HTTPException(409, "只有已发料的称量单可划定补坪范围")
        zone = ticket["zone_code"]
        catalog.lookup(conn, zones, zone, lock=True)
        existing = select(patches.c.code).where(patches.c.code == code)
        if conn.execute(existing).scalar() is not None:
            raise HTTPException(409, "此称量单已有补坪工单")
        conflicts = (
            select(holds.c.grid_ref)
            .where(holds.c.zone_code == zone, holds.c.grid_ref.in_(form.grid_refs))
            .order_by(holds.c.grid_ref)
        )
        taken = conn.execute(conflicts).scalars().all()
        if taken:
            raise HTTPException(409, f"网格正在补坪：{'、'.join(taken)}")
        patch = {"code": code, "zone_code": zone, "planned_by": user["sub"], "laid_by": None, "opened_by": None}
        conn.execute(patches.insert().values(**patch))
        for grid_ref in sorted(form.grid_refs):
            result = conn.execute(cells.insert().values(patch_code=code, grid_ref=grid_ref))
            conn.execute(holds.insert().values(zone_code=zone, grid_ref=grid_ref, cell_id=result.inserted_primary_key[0]))
        record(conn, user, f"patch.mark:{code}")
        return _assemble_detail(conn, patch)


def lay(code, user):
    with transaction() as conn:
        patch = catalog.lookup(conn, patches, code, lock=True)
        if patch["laid_by"] is not None:
            raise HTTPException(409, "此补坪工单已登记实铺")
        conn.execute(patches.update().where(patches.c.code == code).values(laid_by=user["sub"]))
        record(conn, user, f"patch.lay:{code}")
        return _assemble_detail(conn, patch | {"laid_by": user["sub"]})


def measure(code, grid_ref, form, user):
    with transaction() as conn:
        patch = catalog.lookup(conn, patches, code, lock=True)
        if patch["laid_by"] is None or patch["opened_by"] is not None:
            raise HTTPException(409, "只能测读已实铺且尚未开场的网格")
        query = select(cells.c.id).where(cells.c.patch_code == code, cells.c.grid_ref == grid_ref)
        cell_id = conn.execute(query).scalar()
        if cell_id is None:
            raise HTTPException(404, "网格不在这张工单内")
        history = (
            select(readings)
            .where(readings.c.cell_id == cell_id, readings.c.kind == "routine")
            .order_by(readings.c.id.desc()).limit(1)
        )
        previous = conn.execute(history).mappings().first()
        if previous is not None and previous["passed"]:
            raise HTTPException(409, "该网格最新常规测读已合格")
        repeat_of = previous["id"] if previous is not None else None
        if form.repeat_of != repeat_of:
            raise HTTPException(409, "复测必须关联同一网格的最新失败常规读数")

        depths = (form.first_mm, form.middle_mm, form.last_mm)
        passed, spread = _depths_pass(depths)
        result = conn.execute(readings.insert().values(
            cell_id=cell_id, kind="routine", **form.model_dump(), spread_mm=spread,
            passed=passed, measured_by=user["sub"],
        ))
        # 本格重新测读：旧接缝复核单依据的读数已被取代，先作废再按新读数单独判定
        conn.execute(seam_reviews.update().where(
            seam_reviews.c.cell_id == cell_id, seam_reviews.c.state == "open",
        ).values(state="voided", voided_reason="cell_reread", resolved_at=datetime.now(timezone.utc)))
        new_reading = dict(conn.execute(
            select(readings).where(readings.c.id == result.inserted_primary_key[0])
        ).mappings().first())
        _evaluate_cell_seam(conn, patch["zone_code"], {"id": cell_id, "grid_ref": grid_ref}, new_reading)
        # 邻格读数更新：整区其余待开场补坪单的接缝复核单过期并重算（不含本格）
        _recompute_zone_seam(conn, patch["zone_code"], "neighbor_reading_updated", skip_cell_id=cell_id)
        record(conn, user, f"patch.measure:{code}:{grid_ref}")
        return _assemble_detail(conn, patch)


def seam_review(code, grid_ref, form, user):
    """接缝厚差换人复核：复核读数须过原标准，且不入邻格预测。"""
    with transaction() as conn:
        patch = catalog.lookup(conn, patches, code, lock=True)
        if patch["laid_by"] is None or patch["opened_by"] is not None:
            raise HTTPException(409, "只能复核已实铺且尚未开场的网格")
        cell = conn.execute(
            select(cells).where(cells.c.patch_code == code, cells.c.grid_ref == grid_ref)
        ).mappings().first()
        if cell is None:
            raise HTTPException(404, "网格不在这张工单内")
        review = conn.execute(
            select(seam_reviews).where(
                seam_reviews.c.cell_id == cell["id"], seam_reviews.c.state == "open"
            ).order_by(seam_reviews.c.id.desc())
        ).mappings().first()
        if review is None:
            raise HTTPException(409, "该网格没有待处理的接缝厚差复核单")
        trigger = conn.execute(
            select(readings).where(readings.c.id == review["trigger_reading_id"])
        ).mappings().first()
        if trigger["measured_by"] == user["sub"]:
            raise HTTPException(409, "接缝厚差须换人复核，不能由原测量人复核")

        depths = (form.first_mm, form.middle_mm, form.last_mm)
        passed, spread = _depths_pass(depths)
        result = conn.execute(readings.insert().values(
            cell_id=cell["id"], kind="seam_review", first_mm=form.first_mm,
            middle_mm=form.middle_mm, last_mm=form.last_mm, spread_mm=spread,
            passed=passed, repeat_of=review["trigger_reading_id"], measured_by=user["sub"],
        ))
        if not passed:
            record(conn, user, f"patch.seam_review_failed:{code}:{grid_ref}")
            return _assemble_detail(conn, patch)
        now = datetime.now(timezone.utc)
        conn.execute(seam_reviews.update().where(seam_reviews.c.id == review["id"]).values(
            state="resolved", review_reading_id=result.inserted_primary_key[0], resolved_at=now,
        ))
        record(conn, user, f"patch.seam_review:{code}:{grid_ref}")
        return _assemble_detail(conn, patch)


def open_pitch(code, user):
    with transaction() as conn:
        patch = catalog.lookup(conn, patches, code, lock=True)
        if patch["opened_by"] is not None:
            raise HTTPException(409, "此补坪单已签开场")
        detail = _assemble_detail(conn, patch)
        if patch["laid_by"] is None or detail["passed_cells"] != len(detail["cells"]):
            raise HTTPException(409, "须完成实铺、全部网格常规测读合格且无待处理接缝复核后才能开场")
        # 签字落账时冻结各格当时的读数预测快照；开场认此刻状态
        for cell in detail["cells"]:
            conn.execute(cells.update().where(cells.c.id == cell["id"]).values(
                snapshot_prediction_mm=cell["seam_prediction_mm"],
                snapshot_deviation_mm=cell["seam_deviation_mm"],
            ))
        conn.execute(patches.update().where(patches.c.code == code).values(opened_by=user["sub"]))
        conn.execute(delete(holds).where(holds.c.cell_id.in_([cell["id"] for cell in detail["cells"]])))
        record(conn, user, f"patch.open:{code}")
        detail["patch"]["opened_by"] = user["sub"]
        for cell in detail["cells"]:
            cell["snapshot_prediction_mm"] = cell["seam_prediction_mm"]
            cell["snapshot_deviation_mm"] = cell["seam_deviation_mm"]
        return detail
