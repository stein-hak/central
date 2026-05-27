-- Centralized 3x-ui Subscription Manager Database Schema

-- Nodes table - 3x-ui panel instances
CREATE TABLE IF NOT EXISTS nodes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    url VARCHAR(512) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    username VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON COLUMN nodes.url IS 'API URL (Tailscale IP or internal address, e.g., https://100.64.1.5:2053)';
COMMENT ON COLUMN nodes.domain IS 'Public domain for VLESS URLs (e.g., vienna.example.com)';

-- Clients table - VPN users
CREATE TABLE IF NOT EXISTS clients (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Keys table - VLESS keys per client per node
CREATE TABLE IF NOT EXISTS keys (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    inbound_id INTEGER NOT NULL,
    uuid UUID NOT NULL,
    vless_url TEXT NOT NULL,
    manual BOOLEAN DEFAULT FALSE,  -- Migration 002: distinguish auto-generated vs manual keys
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(client_id, node_id, inbound_id)
);

-- Migration 003: Users table for user management system
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    name VARCHAR(255),
    payment_status INTEGER DEFAULT 1,  -- 1=TEST, 2=PAID, 3=NOT_PAID, 4=PROMO
    limit_ip INTEGER DEFAULT 0,  -- 0 = unlimited
    tag VARCHAR(100),
    payment_date DATE,
    renewal_date DATE,  -- For TEST users: created_at + 72 hours
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Add user_id to clients table (1:1 relationship)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE;

-- Migration 004: Subscription domains table for multi-domain support
CREATE TABLE IF NOT EXISTS subscription_domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) UNIQUE NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    is_primary BOOLEAN DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert default subscription domain
INSERT INTO subscription_domains (domain, enabled, is_primary, notes)
VALUES ('localhost:8001', TRUE, TRUE, 'Default subscription domain - update this!')
ON CONFLICT (domain) DO NOTHING;

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_clients_email ON clients(email);
CREATE INDEX IF NOT EXISTS idx_keys_client_id ON keys(client_id);
CREATE INDEX IF NOT EXISTS idx_keys_node_id ON keys(node_id);
CREATE INDEX IF NOT EXISTS idx_keys_manual ON keys(manual);
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_subscription_domains_domain ON subscription_domains(domain);
CREATE INDEX IF NOT EXISTS idx_subscription_domains_enabled ON subscription_domains(enabled);

-- Create read-only user for subscription service
CREATE USER sub_readonly WITH PASSWORD 'sub_readonly_password';
GRANT CONNECT ON DATABASE xui_central TO sub_readonly;
GRANT USAGE ON SCHEMA public TO sub_readonly;
GRANT SELECT ON clients, keys, nodes, users, subscription_domains TO sub_readonly;
