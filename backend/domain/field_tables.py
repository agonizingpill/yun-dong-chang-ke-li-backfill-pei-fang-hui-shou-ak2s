from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Integer, String, Table, UniqueConstraint

from core.db import metadata


patches = Table(
    "field_patches", metadata,
    Column("code", String(24), ForeignKey("refill_tickets.code"), primary_key=True),
    Column("zone_code", String(24), ForeignKey("playing_zones.code"), nullable=False),
    Column("planned_by", String(80), nullable=False),
    Column("laid_by", String(80)),
    Column("opened_by", String(80)),
    CheckConstraint("opened_by IS NULL OR laid_by IS NOT NULL"),
)

cells = Table(
    "patch_cells", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("patch_code", String(24), ForeignKey("field_patches.code"), nullable=False),
    Column("grid_ref", String(2), nullable=False),
    # 开场签字时冻结的接缝预测快照；待开场工单始终实时重算，不留值
    Column("snapshot_prediction_mm", Float),
    Column("snapshot_deviation_mm", Float),
    UniqueConstraint("patch_code", "grid_ref"),
)

holds = Table(
    "active_patch_cells", metadata,
    Column("zone_code", String(24), ForeignKey("playing_zones.code"), primary_key=True),
    Column("grid_ref", String(2), primary_key=True),
    Column("cell_id", Integer, ForeignKey("patch_cells.id"), nullable=False, unique=True),
)

readings = Table(
    "patch_depth_readings", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cell_id", Integer, ForeignKey("patch_cells.id"), nullable=False, index=True),
    # routine = 现场常规测厚（参与邻格预测）；seam_review = 接缝厚差换人复核（不入预测）
    Column("kind", String(12), nullable=False),
    Column("first_mm", Integer, nullable=False),
    Column("middle_mm", Integer, nullable=False),
    Column("last_mm", Integer, nullable=False),
    Column("spread_mm", Integer, nullable=False),
    Column("passed", Boolean, nullable=False),
    # 常规复测关联上一条失败常规读数；接缝复核关联触发复核的常规读数，均可多条留痕
    Column("repeat_of", Integer, ForeignKey("patch_depth_readings.id")),
    Column("measured_by", String(80), nullable=False),
    CheckConstraint("kind IN ('routine','seam_review')"),
    CheckConstraint("first_mm BETWEEN 0 AND 35 AND middle_mm BETWEEN 0 AND 35 AND last_mm BETWEEN 0 AND 35"),
    CheckConstraint("spread_mm BETWEEN 0 AND 35"),
)

seam_reviews = Table(
    "patch_seam_reviews", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cell_id", Integer, ForeignKey("patch_cells.id"), nullable=False, index=True),
    Column("trigger_reading_id", Integer, ForeignKey("patch_depth_readings.id"), nullable=False),
    Column("prediction_mm", Float, nullable=False),
    Column("observed_mean_mm", Float, nullable=False),
    Column("deviation_mm", Float, nullable=False),
    # open = 待换人复核；resolved = 复核通过已解决；voided = 邻格读数更新致复核依据过期
    Column("state", String(10), nullable=False),
    Column("review_reading_id", Integer, ForeignKey("patch_depth_readings.id")),
    Column("voided_reason", String(40)),
    Column("created_by", String(80), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("resolved_at", DateTime),
    CheckConstraint("state IN ('open','resolved','voided')"),
    CheckConstraint("state <> 'resolved' OR review_reading_id IS NOT NULL"),
    CheckConstraint("state = 'open' OR resolved_at IS NOT NULL"),
)
