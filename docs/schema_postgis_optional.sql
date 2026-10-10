-- PostgreSQL + PostGIS: durable memory model starter
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS admin_units (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(32) UNIQUE,
  name TEXT NOT NULL,
  level VARCHAR(24) NOT NULL CHECK (level IN ('country','province','commune','ward','special')),
  parent_id BIGINT REFERENCES admin_units(id),
  effective_from DATE,
  effective_to DATE,
  boundary geometry(MultiPolygon, 4326),
  source_name TEXT,
  source_url TEXT,
  source_updated_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_units_boundary ON admin_units USING GIST(boundary);
CREATE INDEX IF NOT EXISTS idx_admin_units_name_trgm ON admin_units USING GIN(name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS places (
  id UUID PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT,
  category TEXT,
  address_original TEXT,
  address_normalized TEXT,
  admin_unit_id BIGINT REFERENCES admin_units(id),
  location geometry(Point, 4326),
  phone TEXT,
  website TEXT,
  opening_hours JSONB NOT NULL DEFAULT '{}'::jsonb,
  attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes TEXT,
  operational_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (operational_status IN ('unknown','active','inactive','closed','unverified')),
  verification_status TEXT NOT NULL DEFAULT 'unverified'
    CHECK (verification_status IN ('unverified','verified','rejected','needs_review')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_places_location ON places USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_places_name_trgm ON places USING GIN(canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_places_category ON places(category);
CREATE INDEX IF NOT EXISTS idx_places_admin_unit ON places(admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_places_attributes ON places USING GIN(attributes);

-- Keep each source observation, rather than overwriting provenance.
CREATE TABLE IF NOT EXISTS place_sources (
  id BIGSERIAL PRIMARY KEY,
  place_id UUID NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  source_name TEXT NOT NULL,
  source_record_id TEXT,
  source_url TEXT,
  license_or_terms TEXT,
  collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_updated_at TIMESTAMPTZ,
  raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  content_hash TEXT,
  is_current BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE(source_name, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_place_sources_place ON place_sources(place_id);
CREATE INDEX IF NOT EXISTS idx_place_sources_hash ON place_sources(content_hash);

CREATE TABLE IF NOT EXISTS place_media (
  id BIGSERIAL PRIMARY KEY,
  place_id UUID NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  media_type TEXT NOT NULL CHECK (media_type IN ('photo','logo','document','other')),
  uri TEXT NOT NULL,
  caption TEXT,
  rights_basis TEXT,
  source_name TEXT,
  captured_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_place_media_place ON place_media(place_id);

CREATE TABLE IF NOT EXISTS memory_records (
  id UUID PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  content TEXT NOT NULL,
  structured_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  confidence NUMERIC(4,3) CHECK (confidence >= 0 AND confidence <= 1),
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  source_name TEXT,
  source_record_id TEXT,
  source_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  supersedes_id UUID REFERENCES memory_records(id),
  is_current BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_memory_entity ON memory_records(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_memory_structured ON memory_records USING GIN(structured_data);
CREATE INDEX IF NOT EXISTS idx_memory_current ON memory_records(is_current, entity_type);

CREATE TABLE IF NOT EXISTS change_log (
  id BIGSERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  change_type TEXT NOT NULL,
  changed_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
  before_data JSONB,
  after_data JSONB,
  source_name TEXT,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_change_entity ON change_log(entity_type, entity_id, occurred_at DESC);
