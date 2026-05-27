-- Migration: Add subscription_domains table for managing subscription service domains
-- Purpose: Allow multiple domains for subscription service to handle domain blocks

CREATE TABLE IF NOT EXISTS subscription_domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) UNIQUE NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    is_primary BOOLEAN DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_subscription_domains_domain ON subscription_domains(domain);
CREATE INDEX IF NOT EXISTS idx_subscription_domains_enabled ON subscription_domains(enabled);

-- Insert current SUBSCRIPTION_URL as default if not exists
-- Users should update this with their actual domain
INSERT INTO subscription_domains (domain, enabled, is_primary, notes)
VALUES ('localhost:8001', TRUE, TRUE, 'Default subscription domain - update this!')
ON CONFLICT (domain) DO NOTHING;
