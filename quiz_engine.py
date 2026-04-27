"""
quiz_engine.py
──────────────
Self-contained quiz logic: AI question generation, the async game loop,
and per-question scoring. Import this into main.py and call the two
public coroutines:

    questions = await generate_questions(topic)
    await run_game_loop(code, challenges, game_state, connections, pool, broadcast_fn)

On-chain resolution
───────────────────
After the game ends the engine calls the QuizHub contract as the resolver wallet:
  • Winner found  → setWinner(quizId, winnerAddress)
  • Tie           → refundQuiz(quizId)   (each player can then call emergencyRefund)

Required env vars:
  RESOLVER_PRIVATE_KEY   — private key of the wallet set as `resolver` in the contract
  QUIZ_HUB_CONTRACT      — deployed QuizHub contract address on Celo
  CELO_RPC_URL           — defaults to https://forno.celo.org
"""
from __future__ import annotations
import os, json, asyncio, logging
from typing import Dict, List, Callable, Awaitable

import httpx
import asyncpg
from web3 import Web3
from eth_account import Account

from notifications import NotificationService

logger = logging.getLogger(__name__)

# ─── Gemini config ────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/gemini-2.5-flash:generateContent"
)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
# ─── On-chain config ──────────────────────────────────────────────────────────

RESOLVER_PRIVATE_KEY = os.getenv("RESOLVER_PRIVATE_KEY", "")
QUIZ_HUB_CONTRACT    = os.getenv("QUIZ_HUB_CONTRACT", "")
CELO_RPC_URL         = os.getenv("CELO_RPC_URL", "https://forno.celo.org")

# Minimal ABI — only the functions the resolver wallet needs to call
_RESOLVER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "quizId",  "type": "bytes32"},
            {"internalType": "address", "name": "winner",  "type": "address"},
        ],
        "name": "setWinner",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "quizId", "type": "bytes32"},
        ],
        "name": "refundQuiz",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# ─── Round config ─────────────────────────────────────────────────────────────

ROUND_CONFIG = [
    {"type": "easy",   "timeLimit": 7},
    {"type": "medium", "timeLimit": 10},
    {"type": "hard",   "timeLimit": 13},
]

BASE_POINTS = 500   # guaranteed for any correct answer
SPEED_BONUS = 500   # max extra for answering instantly


# ─── AI Question Generation ───────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> dict:
    """Call Gemini and return parsed rounds dict. Raises on any error."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
        )
        resp.raise_for_status()
        raw  = resp.json()
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)


async def _call_groq(prompt: str) -> dict:
    """Call Groq (OpenAI-compatible) and return parsed rounds dict. Raises on any error."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {
                        "role":    "system",
                        "content": "You are a quiz generator. Return ONLY valid JSON, no markdown fences.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
            },
        )
        resp.raise_for_status()
        raw  = resp.json()
        text = raw["choices"][0]["message"]["content"]
        # Strip accidental markdown fences just in case
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)


async def generate_questions(topic: str, total_count: int = 15) -> dict:
    """
    total_count:  15... up to 30.
    """
    per_round = total_count // 3
    
    prompt = f"""
Generate a 1v1 quiz challenge about "{topic}".
Create exactly {total_count} multiple-choice questions divided into 3 rounds:
- Round 1 (Easy):   {per_round} questions — straightforward recall.
- Round 2 (Medium): {per_round} questions — requires understanding.
- Round 3 (Hard):   {per_round} questions — tricky, nuanced, or analytical.

Return ONLY valid JSON:
{{
  "rounds": [
    {{ "type": "easy",   "timeLimit": 7,  "questions": [ ... ] }},
    {{ "type": "medium", "timeLimit": 10, "questions": [ ... ] }},
    {{ "type": "hard",   "timeLimit": 13, "questions": [ ... ] }}
  ]
}}
Each question object must follow this exact shape:
{{
  "question": "Question text here?",
  "options":  [ {{"id":"A","text":"..."}}, {{"id":"B","text":"..."}},
                {{"id":"C","text":"..."}}, {{"id":"D","text":"..."}} ],
  "correctId": "A"
}}
"""
    data = None

    # ── Try Gemini ────────────────────────────────────────────────────────────
    if GEMINI_API_KEY:
        try:
            logger.info("generate_questions: trying Gemini...")
            data = await _call_gemini(prompt)
            logger.info("generate_questions: Gemini succeeded.")
        except Exception as e:
            logger.warning("generate_questions: Gemini failed (%s), falling back to Groq.", e)

    # ── Fallback: Groq ────────────────────────────────────────────────────────
    if data is None:
        if not GROQ_API_KEY:
            raise RuntimeError("Gemini unavailable and GROQ_API_KEY is not set — cannot generate questions.")
        try:
            logger.info("generate_questions: trying Groq fallback...")
            data = await _call_groq(prompt)
            logger.info("generate_questions: Groq succeeded.")
        except Exception as e:
            logger.error("generate_questions: Groq also failed: %s", e)
            raise RuntimeError(f"Both Gemini and Groq failed. Last error: {e}") from e

    # ── Enforce round config regardless of which provider answered ────────────
    for i, rnd in enumerate(data["rounds"]):
        rnd["timeLimit"] = ROUND_CONFIG[i]["timeLimit"]
        rnd["type"]      = ROUND_CONFIG[i]["type"]

    return data

def strip_answers(rounds_data: dict) -> dict:
    """
    Returns a copy of rounds_data with correctId removed from every question.
    Use this when sending question payloads over WebSocket so clients can't cheat.
    """
    import copy
    safe = copy.deepcopy(rounds_data)
    for rnd in safe.get("rounds", []):
        for q in rnd.get("questions", []):
            q.pop("correctId", None)
    return safe


# ─── Scoring ──────────────────────────────────────────────────────────────────

def calc_points(time_taken: float, time_limit: int) -> int:
    """
    Points for a CORRECT answer.
    Formula: base + speed_bonus * (time_remaining / time_limit)
    Min = BASE_POINTS  (answered at last millisecond)
    Max = BASE_POINTS + SPEED_BONUS  (answered instantly)
    """
    time_remaining = max(0.0, time_limit - time_taken)
    speed_factor   = time_remaining / time_limit
    return BASE_POINTS + int(SPEED_BONUS * speed_factor)


# ─── On-Chain Resolution ──────────────────────────────────────────────────────

def _get_quiz_id(code: str) -> bytes:
    """Derive the bytes32 quizId the contract uses — keccak256 of the challenge code."""
    return Web3.keccak(text=code)


def _build_w3() -> tuple[Web3, any]:
    """
    Returns (w3, resolver_account).
    Raises RuntimeError if env vars are missing so the caller can handle gracefully.
    """
    if not RESOLVER_PRIVATE_KEY:
        raise RuntimeError("RESOLVER_PRIVATE_KEY env var not set")
    if not QUIZ_HUB_CONTRACT:
        raise RuntimeError("QUIZ_HUB_CONTRACT env var not set")

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    account = Account.from_key(RESOLVER_PRIVATE_KEY)
    return w3, account


async def _set_winner_on_chain(code: str, winner_address: str) -> None:
    """
    Calls setWinner(quizId, winner) on the QuizHub contract as the resolver wallet.
    After this the winner can call claimReward(quizId) from the frontend.
    Runs in a thread-pool executor so it doesn't block the event loop.
    """
    def _send() -> str:
        w3, account = _build_w3()
        contract    = w3.eth.contract(
            address=Web3.to_checksum_address(QUIZ_HUB_CONTRACT),
            abi=_RESOLVER_ABI,
        )
        quiz_id     = _get_quiz_id(code)
        winner_cs   = Web3.to_checksum_address(winner_address)

        tx = contract.functions.setWinner(quiz_id, winner_cs).build_transaction({
            "from":  account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas":   120_000,
            # Let web3.py estimate gasPrice / maxFeePerGas for Celo
        })
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise RuntimeError(f"setWinner tx reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    loop = asyncio.get_event_loop()
    try:
        tx_hash = await loop.run_in_executor(None, _send)
        logger.info("setWinner on-chain OK  code=%s  winner=%s  tx=%s",
                    code, winner_address, tx_hash)
    except Exception as exc:
        # Log but don't crash the game — winner is already recorded in the DB.
        # The backend can retry via an admin endpoint or cron job.
        logger.error("setWinner on-chain FAILED  code=%s  error=%s", code, exc)


async def _refund_quiz_on_chain(code: str) -> None:
    """
    Calls refundQuiz(quizId) on the QuizHub contract for a tied game.
    After this both players can call emergencyRefund(quizId) from the frontend.
    """
    def _send() -> str:
        w3, account = _build_w3()
        contract    = w3.eth.contract(
            address=Web3.to_checksum_address(QUIZ_HUB_CONTRACT),
            abi=_RESOLVER_ABI,
        )
        quiz_id = _get_quiz_id(code)

        tx = contract.functions.refundQuiz(quiz_id).build_transaction({
            "from":  account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas":   120_000,
        })
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise RuntimeError(f"refundQuiz tx reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    loop = asyncio.get_event_loop()
    try:
        tx_hash = await loop.run_in_executor(None, _send)
        logger.info("refundQuiz on-chain OK  code=%s  tx=%s", code, tx_hash)
    except Exception as exc:
        logger.error("refundQuiz on-chain FAILED  code=%s  error=%s", code, exc)


# ─── Game Loop ────────────────────────────────────────────────────────────────

BroadcastFn = Callable[[str, dict], Awaitable[None]]


async def run_game_loop(
    code:       str,
    challenges: Dict[str, dict],
    game_state: Dict[str, dict],
    pool:       asyncpg.Pool,
    broadcast:  BroadcastFn,
    notif:      "NotificationService",   # ← add this
) -> None:
    """
    Drives a full match:
      1. Countdown
      2. Loop rounds → questions → collect answers → score → reveal
      3. Determine winner, persist to DB, resolve on-chain, notify players
    """
    challenge = challenges[code]
    state     = game_state[code]

    challenge["status"] = "active"
    await _mark_started(pool, code)

    # ── Pre-game countdown ────────────────────────────────────────────────────
    await broadcast(code, {"type": "game_start", "message": "Challenge starting in 3…"})
    await asyncio.sleep(3)

    # ── Rounds ───────────────────────────────────────────────────────────────
    rounds = challenge["rounds"]

    for r_idx, rnd in enumerate(rounds):
        await broadcast(code, {
            "type":        "round_announce",
            "round":       rnd["type"],
            "roundIndex":  r_idx,
            "totalRounds": len(rounds),
            "timeLimit":   rnd["timeLimit"],
        })
        await asyncio.sleep(3)

        for q_idx, q in enumerate(rnd["questions"]):
            # Send question WITHOUT the correct answer
            await broadcast(code, {
                "type":           "question",
                "roundIndex":     r_idx,
                "questionIndex":  q_idx,
                "totalQuestions": len(rnd["questions"]),
                "data": {
                    "question":  q["question"],
                    "options":   q["options"],
                    "timeLimit": rnd["timeLimit"],
                },
            })

            # Wait for the time window
            await asyncio.sleep(rnd["timeLimit"])

            # ── Score this question ───────────────────────────────────────
            ans_key       = f"{r_idx}_{q_idx}"
            round_answers = state["answers"].get(ans_key, {})
            q_scores: Dict[str, int] = {}

            for wallet, ans in round_answers.items():
                correct = ans["answerId"] == q["correctId"]
                pts     = calc_points(ans["timeTaken"], rnd["timeLimit"]) if correct else 0
                challenge["players"][wallet]["points"] += pts
                q_scores[wallet] = pts

                await _save_answer(
                    pool, code, wallet, r_idx, q_idx,
                    ans["answerId"], correct, ans["timeTaken"], pts,
                )

            await broadcast(code, {
                "type":           "question_end",
                "roundIndex":     r_idx,
                "questionIndex":  q_idx,
                "correctId":      q["correctId"],
                "questionScores": q_scores,
                "totalScores": {
                    w: p["points"]
                    for w, p in challenge["players"].items()
                },
            })
            await asyncio.sleep(3)   # brief pause before next question

        # ── End-of-round summary ──────────────────────────────────────────
        await broadcast(code, {
            "type":      "round_end",
            "roundIndex": r_idx,
            "roundType":  rnd["type"],
            "scores":    {w: p["points"] for w, p in challenge["players"].items()},
        })
        await asyncio.sleep(4)

    # ── Determine Winner ──────────────────────────────────────────────────────
    players = challenge["players"]
    scores  = {w: p["points"] for w, p in players.items()}
    wallets = list(scores.keys())

    if scores[wallets[0]] == scores[wallets[1]]:
        winner  = None
        outcome = "tie"
    else:
        winner  = max(scores, key=scores.__getitem__)
        outcome = "winner"

    challenge["status"] = "finished"
    challenge["winner"] = winner

    # ── Persist result to DB ──────────────────────────────────────────────────
    await _mark_finished(pool, code, winner)

    # ── Resolve on-chain (non-blocking — errors are logged, not raised) ───────
    if outcome == "winner" and winner:
        # setWinner → winner can now call claimReward() from the frontend
        asyncio.create_task(_set_winner_on_chain(code, winner))
    else:
        # Tie → refundQuiz → both players can call emergencyRefund() from the frontend
        asyncio.create_task(_refund_quiz_on_chain(code))

    stake = challenge.get("stakeAmount", 0)
    token = challenge.get("tokenSymbol", "")

    if outcome == "winner" and winner:
        loser = next(w for w in wallets if w != winner)
        # Persists to DB inbox AND pushes live if connected
        await notif.notify_game_over(code, winner, loser, stake, token)
    else:
        # Tie — notify both
        for wallet in wallets:
            await notif.send(
                wallet,
                "game_over",
                "🤝 It's a tie!",
                "The match ended in a draw. Both stakes will be refunded.",
                {"code": code},
            )

    # WebSocket broadcast for the live game UI (keep this too)
    final_payload = {
        "type":    "game_over",
        "outcome": outcome,
        "winner":  winner,
        "finalScores": {
            w: {
                "username": players[w]["username"],
                "points":   players[w]["points"],
            }
            for w in wallets
        },
        "canRematch": True,
    }
    await broadcast(code, final_payload)
    logger.info("Game %s finished. Winner: %s", code, winner or "TIE")


# ─── DB Helpers ───────────────────────────────────────────────────────────────

async def _mark_started(pool: asyncpg.Pool, code: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_challenges SET status='active', started_at=NOW() WHERE code=$1",
            code,
        )
async def _update_player_stats(pool, winner: str, loser: str) -> None:
    """Increment total_duels for both players, total_wins for winner only."""
    async with pool.acquire() as conn:
        # Both players played one duel
        await conn.execute(
            "UPDATE players SET total_duels = COALESCE(total_duels, 0) + 1 WHERE wallet_address = $1",
            winner,
        )
        await conn.execute(
            "UPDATE players SET total_duels = COALESCE(total_duels, 0) + 1 WHERE wallet_address = $1",
            loser,
        )
        # Only winner gets a win
        await conn.execute(
            "UPDATE players SET total_wins = COALESCE(total_wins, 0) + 1 WHERE wallet_address = $1",
            winner,
        )

async def _mark_finished(pool: asyncpg.Pool, code: str, winner: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ai_challenges
               SET status='finished', winner_address=$1, finished_at=NOW()
               WHERE code=$2""",
            winner, code,
        )
        # Increment total_duels for ALL players — COALESCE guards against NULL
        await conn.execute(
            """UPDATE players SET total_duels = COALESCE(total_duels, 0) + 1
               WHERE wallet_address IN (
                   SELECT wallet_address FROM challenge_players
                   WHERE challenge_id = (SELECT id FROM ai_challenges WHERE code = $1)
               )""",
            code,
        )
        if winner:
            await conn.execute(
                """UPDATE players
                   SET total_wins = COALESCE(total_wins, 0) + 1
                   WHERE wallet_address = $1""",
                winner,
            )
            await conn.execute(
                """UPDATE players
                   SET total_losses = COALESCE(total_losses, 0) + 1
                   WHERE wallet_address IN (
                       SELECT wallet_address FROM challenge_players
                       WHERE challenge_id = (SELECT id FROM ai_challenges WHERE code = $1)
                         AND wallet_address != $2
                   )""",
                code, winner,
            )
            
async def _save_answer(
    pool:       asyncpg.Pool,
    code:       str,
    wallet:     str,
    r_idx:      int,
    q_idx:      int,
    answer_id:  str,
    is_correct: bool,
    time_taken: float,
    points:     int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO challenge_answers
               (challenge_id, wallet_address, round_index, question_index,
                answer_id, is_correct, time_taken, points_awarded)
               VALUES (
                 (SELECT id FROM ai_challenges WHERE code=$1),
                 $2, $3, $4, $5, $6, $7, $8
               )""",
            code, wallet, r_idx, q_idx, answer_id, is_correct, time_taken, points,
        )