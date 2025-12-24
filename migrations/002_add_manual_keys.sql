-- Add manual column to keys table
-- This distinguishes between auto-generated keys (manual=false) and manually entered keys (manual=true)
-- Manual keys are NOT replicated when nodes are added/removed

ALTER TABLE keys ADD COLUMN IF NOT EXISTS manual BOOLEAN DEFAULT FALSE;

-- Set all existing keys as auto-generated
UPDATE keys SET manual = FALSE WHERE manual IS NULL;

-- Add index for faster queries
CREATE INDEX IF NOT EXISTS idx_keys_manual ON keys(manual);

-- Verification query (comment out after running)
-- SELECT id, client_id, node_id, manual FROM keys LIMIT 10;
