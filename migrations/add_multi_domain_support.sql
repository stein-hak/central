-- ============================================================================
-- Migration: Add Multi-Domain Support
-- Date: 2026-05-03
-- Description: Allow multiple domains per node for same physical server
--              Works with both TLS/legacy and Reality inbounds
-- ============================================================================

-- Step 1: Create domains table
-- ============================================================================
CREATE TABLE IF NOT EXISTS domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) NOT NULL UNIQUE,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE domains IS 'Public domains that can be used for VLESS URL generation';
COMMENT ON COLUMN domains.domain IS 'Domain name (e.g., myphonecloud.space)';

-- Step 2: Create node-domain mapping table
-- ============================================================================
CREATE TABLE IF NOT EXISTS node_domains (
    id SERIAL PRIMARY KEY,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,

    -- Configuration
    is_primary BOOLEAN DEFAULT false,
    enabled BOOLEAN DEFAULT true,
    display_name VARCHAR(100),  -- Override node name in subscription (e.g., "node-france", "node-cloudflare")

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(node_id, domain_id)
);

COMMENT ON TABLE node_domains IS 'Many-to-many: one node can serve multiple domains';
COMMENT ON COLUMN node_domains.is_primary IS 'One primary domain per node (uses original node name)';
COMMENT ON COLUMN node_domains.display_name IS 'Override node name in subscription to appear as different server/location (e.g., "node-france", "node-cloudflare"). If NULL, uses original node name.';

-- Create indexes
CREATE INDEX idx_node_domains_node ON node_domains(node_id);
CREATE INDEX idx_node_domains_domain ON node_domains(domain_id);
CREATE INDEX idx_node_domains_enabled ON node_domains(enabled);
CREATE INDEX idx_node_domains_primary ON node_domains(is_primary);

-- Step 3: Migrate existing domains from nodes table
-- ============================================================================
DO $$
DECLARE
    domains_count INTEGER;
    mappings_count INTEGER;
BEGIN
    -- Insert unique domains from nodes table
    INSERT INTO domains (domain, enabled)
    SELECT DISTINCT domain, true
    FROM nodes
    WHERE domain IS NOT NULL AND domain != ''
    ON CONFLICT (domain) DO NOTHING;

    GET DIAGNOSTICS domains_count = ROW_COUNT;

    -- Create node-domain mappings (all marked as primary initially)
    INSERT INTO node_domains (node_id, domain_id, is_primary, enabled, label)
    SELECT n.id, d.id, true, true, 'primary'
    FROM nodes n
    JOIN domains d ON n.domain = d.domain
    WHERE n.domain IS NOT NULL AND n.domain != ''
    ON CONFLICT (node_id, domain_id) DO NOTHING;

    GET DIAGNOSTICS mappings_count = ROW_COUNT;

    RAISE NOTICE 'Migration complete: % unique domains, % node-domain mappings created', domains_count, mappings_count;
END $$;

-- Step 4: Verification queries
-- ============================================================================
-- Summary
SELECT
    'Domains created' as metric,
    COUNT(*) as count
FROM domains
UNION ALL
SELECT
    'Node-domain mappings' as metric,
    COUNT(*) as count
FROM node_domains
UNION ALL
SELECT
    'Primary mappings' as metric,
    COUNT(*) as count
FROM node_domains
WHERE is_primary = true;

-- Show all mappings
SELECT
    n.name as node_name,
    n.url as node_url,
    d.domain,
    nd.label,
    nd.is_primary,
    nd.enabled
FROM node_domains nd
JOIN nodes n ON nd.node_id = n.id
JOIN domains d ON nd.domain_id = d.id
ORDER BY n.name, nd.is_primary DESC, d.domain;

-- ============================================================================
-- IMPORTANT: How Multi-Domain Works
-- ============================================================================
-- 1. VLESS URLs are generated ON-THE-FLY by subscription service
-- 2. NO URL storage in database - only domain configuration is stored
-- 3. Subscription service reads node_domains table and generates URLs dynamically
-- 4. Same client UUID is used across all domains (client exists once on server)
-- 5. Different domain = different URL but same physical connection
--
-- Usage Examples:
-- ============================================================================
-- Add new domain:
--   INSERT INTO domains (domain) VALUES ('newdomain.com');
--
-- Associate domain with node (with custom label):
--   INSERT INTO node_domains (node_id, domain_id, label)
--   SELECT 1, id, 'backup' FROM domains WHERE domain = 'newdomain.com';
--
-- List all domains for node:
--   SELECT d.domain, nd.label, nd.is_primary
--   FROM node_domains nd
--   JOIN domains d ON nd.domain_id = d.id
--   WHERE nd.node_id = 1 AND nd.enabled = true;
--
-- Generate URLs in subscription service:
--   For each key:
--     - Get domains from node_domains where node_id = key.node_id
--     - For each domain: create_vless_url(domain=domain, label=label)
--     - Result: multiple URLs for same client UUID
-- ============================================================================
