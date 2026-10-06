"""在基础服务的 SQLite 边界上扩展灾害装备战备所需的表结构。"""

from __future__ import annotations

from science_strategy_foundation.storage import Database


EQUIPMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment_units (
    equipment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    home_site_id TEXT NOT NULL REFERENCES sites(site_id),
    current_site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    environments_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available', 'maintenance', 'out_of_service')),
    allocation TEXT NOT NULL CHECK(allocation IN ('idle', 'reserved', 'deployed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_records (
    record_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    inspected_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    inspector TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('passed', 'failed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_teams (
    team_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    home_site_id TEXT NOT NULL REFERENCES sites(site_id),
    current_site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    allocation TEXT NOT NULL CHECK(allocation IN ('idle', 'reserved', 'deployed')),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transport_routes (
    route_id TEXT PRIMARY KEY,
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT NOT NULL REFERENCES sites(site_id),
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
    created_at TEXT NOT NULL,
    UNIQUE(from_site_id, to_site_id)
);
CREATE TABLE IF NOT EXISTS mutual_aid_agreements (
    agreement_id TEXT PRIMARY KEY,
    provider_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    requester_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    categories_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    severity TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority >= 0),
    requirement_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL UNIQUE REFERENCES alerts(alert_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    priority INTEGER NOT NULL CHECK(priority >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending', 'reserved', 'waitlisted', 'dispatched',
                                         'active', 'returning', 'completed', 'cancelled')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    expected_end_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    dispatch_id TEXT REFERENCES dispatches(dispatch_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('equipment', 'team')),
    resource_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('primary', 'transport', 'operator', 'replacement')),
    provider_org_id TEXT NOT NULL,
    from_site_id TEXT NOT NULL,
    requires_override INTEGER NOT NULL CHECK(requires_override IN (0, 1)),
    qualification TEXT,
    state TEXT NOT NULL CHECK(state IN ('reserved', 'deployed', 'arrived',
                                        'fulfilled', 'released', 'expired', 'broken')),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commitments_task ON commitments(task_id, state);
CREATE INDEX IF NOT EXISTS idx_commitments_resource ON commitments(resource_type, resource_id, state);
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    override_id TEXT REFERENCES overrides(override_id),
    state TEXT NOT NULL CHECK(state IN ('en_route', 'on_site', 'returning', 'closed', 'superseded')),
    items_json TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    eta_at TEXT NOT NULL,
    arrived_at TEXT,
    finished_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_events (
    event_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    override_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    reason TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    confirmer_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending', 'confirmed', 'expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
"""


def ensure_schema(database: Database) -> None:
    """在既有数据库上补齐装备战备服务的表结构（可重复执行）。"""

    database.connection.executescript(EQUIPMENT_SCHEMA)
