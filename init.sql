-- Centralized 3x-ui Subscription Manager Database Schema

-- Nodes table - 3x-ui panel instances
CREATE TABLE IF NOT EXISTS nodes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    url VARCHAR(512) NOT NULL,
    username VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(client_id, node_id, inbound_id)
);

-- Create indexes
CREATE INDEX idx_clients_email ON clients(email);
CREATE INDEX idx_keys_client_id ON keys(client_id);
CREATE INDEX idx_keys_node_id ON keys(node_id);

-- Create read-only user for subscription service
CREATE USER sub_readonly WITH PASSWORD 'sub_readonly_password';
GRANT CONNECT ON DATABASE xui_central TO sub_readonly;
GRANT USAGE ON SCHEMA public TO sub_readonly;
GRANT SELECT ON clients, keys, nodes TO sub_readonly;
