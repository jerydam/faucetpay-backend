# Quizhub Platform

AI-powered 1v1 staking quiz platform. Two players stake equal amounts, answer
AI-generated trivia in 3 rounds, and the winner claims the pot.

---

## Architecture

```
quiz_platform/
├── schema.sql          ← PostgreSQL schema (run once)
├── models.py           ← Pydantic request/response models
├── quiz_engine.py      ← AI question generation + game loop + scoring
├── notifications.py    ← In-app notification service (DB + live push)
├── main.py             ← FastAPI app, all endpoints + WebSocket handlers
└── requirements.txt
```

---

## Setup

### 1. Environment variables

```env
DATABASE_URL=postgresql://user:pass@host/dbname
GEMINI_API_KEY=your_gemini_key
```

### 2. Database

```bash
psql $DATABASE_URL -f schema.sql
```

### 3. Install & run

```bash
pip install -r requirements.txt
python main.py
# or: uvicorn main:app --reload
```

---

## Challenge Lifecycle

```
Creator                                 Opponent
  │                                        │
  ├─ POST /api/challenge/create            │
  │   ← { code, challenge }               │
  │                                        │
  ├─ WS /ws/challenge/{code}              │
  │   → stake_confirmed (txHash)          │
  │                                        │
  │                          ← notification (public_challenge OR challenge_invite)
  │                                        │
  │                          POST /api/challenge/{code}/join
  │                                        │
  ├─ notification: player_joined          │
  │                                        │
  │   → ready               → ready      │  (both sides send "ready" after staking)
  │                                        │
  │         ←─── game_start ──────────►   │
  │         ←─── round_announce ──────►   │
  │         ←─── question ────────────►   │
  │   → submit_answer       → submit_answer
  │         ←─── question_end ────────►   │
  │               ...9 questions total    │
  │         ←─── game_over ───────────►   │
  │                                        │
  ├─ GET /pending-claims                  │
  ├─ POST /claim                          │
  │                                        │
  ├─ POST /{code}/rematch ────────────── notification: rematch_request
```

---

## WebSocket Message Reference

### `/ws/challenge/{code}` — Game channel

**Client → Server**

| type | fields | description |
|------|--------|-------------|
| `stake_confirmed` | walletAddress, txHash | Player has staked on-chain |
| `ready` | walletAddress | Player ready to start (after stake verified) |
| `submit_answer` | walletAddress, roundIndex, questionIndex, answerId, timeTaken | Submit answer |
| `chat` | walletAddress, username, text | Chat message |

**Server → Client**

| type | description |
|------|-------------|
| `state_sync` | Full challenge snapshot on connect |
| `stake_verified` | Stake tx confirmed |
| `stake_failed` | Stake tx rejected |
| `player_joined` | Opponent joined |
| `player_ready` | A player marked ready |
| `game_start` | Countdown begins |
| `round_announce` | New round starting |
| `question` | Question data (no correct answer) |
| `question_end` | Correct answer + scores revealed |
| `round_end` | Round summary |
| `game_over` | Final scores + winner |
| `chat` | Relayed chat message |

### `/ws/notify/{wallet}` — Live notification channel

Server pushes notification objects and `{ type: "unread_count", count: N }` events.

---

## Scoring

```
correct answer = 500 base points
             + 500 × (time_remaining / time_limit) speed bonus

max per question = 1000 pts  (instant answer)
min per question =  500 pts  (last millisecond)
wrong answer     =    0 pts
```

Round time limits:
- Easy:   7 seconds
- Medium: 10 seconds
- Hard:   13 seconds

---

## Rematch Flow

After any finished game, either player can call:

```
POST /api/challenge/{code}/rematch
{ "requesterWallet": "0x..." }
```

This creates a **new challenge** (new code, fresh AI questions, same topic/stake/chain).
The opponent receives a `rematch_request` notification with the new code.
Both players must stake again for the new game.
