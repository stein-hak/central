-- Add transport filtering to proxies
-- Migration: 007_add_proxy_transport_filter.sql

-- Add allowed_transport column
ALTER TABLE proxies ADD COLUMN IF NOT EXISTS allowed_transport VARCHAR(20) DEFAULT 'xhttp';

-- Update existing proxies to 'xhttp' if NULL
UPDATE proxies SET allowed_transport = 'xhttp' WHERE allowed_transport IS NULL;

-- Add comment
COMMENT ON COLUMN proxies.allowed_transport IS 'Transport type this proxy handles: xhttp, grpc, or tcp';
