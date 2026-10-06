"""灾害装备战备与调拨领域的 SQLite 表结构与连接管理。

存储层建立在基础服务 :mod:`science_strategy_foundation.storage` 的同一组连接、
事务和审计表约定之上，另建战备调拨所需的业务表。
"""

from __future__ import annotations

from science_strategy_foundation.storage import Database

DISASTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS dr_regions (
    region_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_equipment (
    equipment_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    name TEXT NOT NULL,
    capability_code TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    environments_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_certifications (
    certification_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES dr_equipment(equipment_id),
    cert_type TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_maintenance (
    maintenance_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES dr_equipment(equipment_id),
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_crews (
    crew_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_crew_qualifications (
    crew_id TEXT NOT NULL REFERENCES dr_crews(crew_id),
    capability_code TEXT NOT NULL,
    PRIMARY KEY (crew_id, capability_code)
);
CREATE TABLE IF NOT EXISTS dr_vehicles (
    vehicle_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_travel_times (
    origin_region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    destination_region_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL REFERENCES dr_vehicles(vehicle_id),
    minutes INTEGER NOT NULL CHECK(minutes >= 0),
    PRIMARY KEY (origin_region_id, destination_region_id, vehicle_id)
);
CREATE TABLE IF NOT EXISTS dr_mutual_aid (
    agreement_id TEXT PRIMARY KEY,
    holder_region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    counterpart_region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    valid_until TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(holder_region_id, counterpart_region_id)
);
CREATE TABLE IF NOT EXISTS dr_missions (
    mission_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    alarm_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    environment TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 100),
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_mission_requirements (
    requirement_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES dr_missions(mission_id),
    capability_code TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 1),
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_commitments (
    commitment_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES dr_missions(mission_id),
    region_id TEXT NOT NULL REFERENCES dr_regions(region_id),
    kind TEXT NOT NULL CHECK(kind IN ('reserve','override')),
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    environment TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    source_commitment_id TEXT REFERENCES dr_commitments(commitment_id),
    waitlist_entry_id TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dr_commitments_mission ON dr_commitments(mission_id);
CREATE INDEX IF NOT EXISTS idx_dr_commitments_status ON dr_commitments(status);
CREATE TABLE IF NOT EXISTS dr_commitment_items (
    item_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES dr_commitments(commitment_id),
    requirement_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL REFERENCES dr_equipment(equipment_id),
    vehicle_id TEXT NOT NULL REFERENCES dr_vehicles(vehicle_id),
    crew_id TEXT NOT NULL REFERENCES dr_crews(crew_id),
    origin_region_id TEXT NOT NULL,
    eta_minutes INTEGER NOT NULL,
    status TEXT NOT NULL,
    seq INTEGER NOT NULL,
    replaces_item_id TEXT,
    arrived_at TEXT,
    returned_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_dr_items_commitment ON dr_commitment_items(commitment_id);
CREATE TABLE IF NOT EXISTS dr_commitment_events (
    event_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES dr_commitments(commitment_id),
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dr_cev_events ON dr_commitment_events(commitment_id, seq);
CREATE TABLE IF NOT EXISTS dr_resource_locks (
    resource_type TEXT NOT NULL CHECK(resource_type IN ('equipment','vehicle','crew')),
    resource_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL REFERENCES dr_commitments(commitment_id),
    item_id TEXT REFERENCES dr_commitment_items(item_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_dr_locks_commitment ON dr_resource_locks(commitment_id);
CREATE TABLE IF NOT EXISTS dr_waitlist (
    entry_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES dr_missions(mission_id),
    request_json TEXT NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','promoted','cancelled')),
    commitment_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dr_waitlist_rank ON dr_waitlist(status, rank);
CREATE TABLE IF NOT EXISTS dr_overrides (
    override_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES dr_commitments(commitment_id),
    displaced_commitment_id TEXT REFERENCES dr_commitments(commitment_id),
    initiator_id TEXT NOT NULL,
    confirmer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','reverted')),
    reverted_at TEXT,
    created_at TEXT,
    CHECK(initiator_id <> confirmer_id)
);
CREATE TABLE IF NOT EXISTS dr_override_displacements (
    displacement_id TEXT PRIMARY KEY,
    override_id TEXT NOT NULL REFERENCES dr_overrides(override_id),
    commitment_id TEXT NOT NULL REFERENCES dr_commitments(commitment_id),
    item_id TEXT NOT NULL REFERENCES dr_commitment_items(item_id),
    restored INTEGER NOT NULL DEFAULT 0 CHECK(restored IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_dr_displacements_override ON dr_override_displacements(override_id);
CREATE TABLE IF NOT EXISTS dr_request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DisasterDatabase(Database):
    """在基础数据库上建立战备调拨表。"""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(DISASTER_SCHEMA)
