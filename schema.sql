-- ============================================================
-- FaucetDrops Quiz Platform — Full DB Schema
-- ============================================================

-- Players / user profiles (linked by wallet)
CREATE TABLE IF NOT EXISTS players (
    wallet_address   TEXT PRIMARY KEY,          -- lowercase checksum
    username         TEXT NOT NULL,
    avatar_url       TEXT,
    total_wins       INTEGER DEFAULT 0,
    total_losses     INTEGER DEFAULT 0,
    total_earned     NUMERIC(18, 6) DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Core challenge table
CREATE TABLE IF NOT EXISTS ai_challenges (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code             TEXT UNIQUE NOT NULL,       -- 6-char join code
    creator_address  TEXT NOT NULL REFERENCES players(wallet_address),
    topic            TEXT NOT NULL,
    stake_amount     NUMERIC(18, 6) NOT NULL,
    token_symbol     TEXT NOT NULL DEFAULT 'USDC',
    chain_id         INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'waiting'
                        CHECK (status IN ('waiting','active','finished','cancelled')),
    is_public        BOOLEAN DEFAULT TRUE,
    rounds_data      JSONB NOT NULL,             -- full question payload from AI
    winner_address   TEXT REFERENCES players(wallet_address),
    claimed          BOOLEAN DEFAULT FALSE,
    rematch_of       UUID REFERENCES ai_challenges(id), -- chain of rematches
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

CREATE INDEX idx_challenges_status   ON ai_challenges(status);
CREATE INDEX idx_challenges_public   ON ai_challenges(is_public, status);
CREATE INDEX idx_challenges_creator  ON ai_challenges(creator_address);

-- Per-challenge player slot (max 2 rows per challenge)
CREATE TABLE IF NOT EXISTS challenge_players (
    challenge_id     UUID NOT NULL REFERENCES ai_challenges(id) ON DELETE CASCADE,
    wallet_address   TEXT NOT NULL REFERENCES players(wallet_address),
    username         TEXT NOT NULL,
    tx_hash          TEXT,                       -- stake tx hash
    tx_verified      BOOLEAN DEFAULT FALSE,
    points           INTEGER DEFAULT 0,
    ready            BOOLEAN DEFAULT FALSE,
    joined_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (challenge_id, wallet_address)
);

-- Per-question answer log (immutable audit trail)
CREATE TABLE IF NOT EXISTS challenge_answers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id     UUID NOT NULL REFERENCES ai_challenges(id) ON DELETE CASCADE,
    wallet_address   TEXT NOT NULL,
    round_index      SMALLINT NOT NULL,
    question_index   SMALLINT NOT NULL,
    answer_id        TEXT NOT NULL,             -- "A", "B", "C", "D"
    is_correct       BOOLEAN NOT NULL,
    time_taken       NUMERIC(6, 3) NOT NULL,    -- seconds
    points_awarded   INTEGER NOT NULL DEFAULT 0,
    submitted_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_answers_challenge ON challenge_answers(challenge_id);

-- In-app notifications
CREATE TABLE IF NOT EXISTS notifications (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_wallet TEXT NOT NULL,              -- who receives it
    type             TEXT NOT NULL               -- see types below
                        CHECK (type IN (
                            'public_challenge',  -- new public challenge posted
                            'challenge_invite',  -- friend-targeted invite
                            'player_joined',     -- someone joined your challenge
                            'game_starting',     -- both ready, 3s countdown
                            'game_over',         -- your game ended
                            'rematch_request',   -- opponent wants a rematch
                            'reward_available'   -- you can now claim winnings
                        )),
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    data             JSONB,                      -- arbitrary payload (challenge code, etc.)
    is_read          BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notifs_recipient ON notifications(recipient_wallet, is_read, created_at DESC);

-- Public challenge discovery feed (denormalised view for lobby)
CREATE OR REPLACE VIEW public_lobby AS
    SELECT
        c.code,
        c.topic,
        c.stake_amount,
        c.token_symbol,
        c.chain_id,
        c.created_at,
        p.username   AS creator_username,
        p.total_wins AS creator_wins
    FROM ai_challenges c
    JOIN players p ON p.wallet_address = c.creator_address
    WHERE c.status = 'waiting'
      AND c.is_public = TRUE
    ORDER BY c.created_at DESC;
