-- Migration 005: Add proxy support for HAProxy front-end architecture
-- This allows backend nodes to be accessed through proxy servers with fake SNI obfuscation

-- Proxies table - HAProxy front-end servers
CREATE TABLE IF NOT EXISTS proxies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    domain VARCHAR(255) NOT NULL,
    fake_snis TEXT[],
    sni_strategy VARCHAR(20) DEFAULT 'random',
    enabled BOOLEAN DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE proxies IS 'HAProxy front-end servers that provide client access to backend nodes';
COMMENT ON COLUMN proxies.name IS 'Display name shown in subscription (e.g., "US-Server", "EU-Server")';
COMMENT ON COLUMN proxies.domain IS 'Public domain for client connections (e.g., "phone-bliss.tech")';
COMMENT ON COLUMN proxies.fake_snis IS 'Array of fake SNI domains for obfuscation (e.g., ["vk.com", "mail.ru", "ok.ru"])';
COMMENT ON COLUMN proxies.sni_strategy IS 'Strategy for selecting fake SNI: "random", "fixed", or "rotate"';

-- Proxy backends - many-to-many relationship between proxies and nodes
CREATE TABLE IF NOT EXISTS proxy_backends (
    id SERIAL PRIMARY KEY,
    proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    weight INTEGER DEFAULT 1,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proxy_id, node_id)
);

COMMENT ON TABLE proxy_backends IS 'Many-to-many: which backend nodes are behind which proxies';
COMMENT ON COLUMN proxy_backends.weight IS 'Weight for load balancing (used in HAProxy weighted roundrobin)';

-- Add proxy_only flag to nodes
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS proxy_only BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN nodes.proxy_only IS 'If TRUE, node keys are only shown through proxy. Direct access hidden from subscription.';

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_proxy_backends_proxy ON proxy_backends(proxy_id);
CREATE INDEX IF NOT EXISTS idx_proxy_backends_node ON proxy_backends(node_id);
CREATE INDEX IF NOT EXISTS idx_proxy_backends_enabled ON proxy_backends(enabled);
CREATE INDEX IF NOT EXISTS idx_proxies_enabled ON proxies(enabled);
CREATE INDEX IF NOT EXISTS idx_nodes_proxy_only ON nodes(proxy_only);

-- Grant permissions to read-only subscription user
GRANT SELECT ON proxies, proxy_backends TO sub_readonly;
