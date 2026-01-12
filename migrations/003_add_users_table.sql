-- Migration: Add users table and link to clients
-- This implements Phase 0 of user management system

-- Step 1: Create users table
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

-- Step 2: Add indexes for better performance
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

-- Step 3: Add user_id to clients table (nullable for backward compatibility)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- Step 4: Add foreign key constraint
ALTER TABLE clients ADD CONSTRAINT fk_clients_user_id
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Step 5: Add unique constraint (1:1 relationship)
ALTER TABLE clients ADD CONSTRAINT uq_clients_user_id UNIQUE (user_id);

-- Verification queries
SELECT COUNT(*) as users_count FROM users;
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY ordinal_position;
