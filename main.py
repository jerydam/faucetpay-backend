from __future__ import annotations
import os, json, random, string, asyncio, logging, uuid
from typing import Dict, List, Optional
import datetime
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from web3 import Web3
import httpx
import asyncpg
from supabase import create_client, Client
from models import (
    CheckAvailabilityRequest, CreateChallengeRequest, JoinChallengeRequest,
    RematachRequest, ClaimRequest, UpdateProfileRequest, UserProfile, SyncProfileRequest, StakeOfferRequest
)
from abi import QUIZ_HUB_ABI
from eth_account import Account
import re
from pydantic import BaseModel



logger = logging.getLogger(__name__)
load_dotenv()

_required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "DATABASE_URL", "QUIZ_HUB_CONTRACT"]
_missing  = [v for v in _required if not os.getenv(v)]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

from quiz_engine import generate_questions, run_game_loop, strip_answers
from notifications import NotificationService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Quizhub Quiz Platform", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BACKEND_ADDRESS = os.getenv("BACKEND_ADDRESS")        # fixed typo: was BACKEN_ADDRESS
PRIVATE_KEY     = os.getenv("RESOLVER_PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("QUIZ_HUB_CONTRACT")
DATABASE_URL     = os.getenv("DATABASE_URL")

RPC_URLS = {
    42220: "https://forno.celo.org",
    8453:  "https://mainnet.base.org",
    1135:  "https://rpc.api.lisk.com",
}

# ─── Global In-Memory State ───────────────────────────────────────────────────
challenges:  Dict[str, dict]            = {}
game_state:  Dict[str, dict]            = {}
connections: Dict[str, List[WebSocket]] = {}
notify_conn: Dict[str, List[WebSocket]] = {}
offers:      Dict[str, dict]            = {}
pool: asyncpg.Pool = None
notif: NotificationService = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def smart_checksum(address: str) -> str:
    if not address:
        return ""
    return Web3.to_checksum_address(address)

def normalize_db_address(address: str) -> str:
    if not address:
        return ""
    return address.lower()

def make_code(k: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=k))

def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr) if addr else addr


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global pool, notif

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    pool = await asyncpg.create_pool(
        dsn=db_url,
        min_size=1,
        max_size=5,
        statement_cache_size=0,
        ssl="require",
        timeout=30,
        command_timeout=60,
        server_settings={"application_name": "quizhub_backend"},
    )
    notif = NotificationService(pool, notify_conn)
    logger.info("🚀 Quiz Platform Started")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


# ─── On-chain helpers ─────────────────────────────────────────────────────────

async def verify_stake_tx(tx_hash: str, expected_quiz_id: str) -> bool:
    rpc = RPC_URLS[42220]
    async with httpx.AsyncClient() as client:
        resp = await client.post(rpc, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash]
        })
        receipt = resp.json().get("result")
        if not receipt or receipt.get("status") != "0x1":
            return False
        if receipt.get("to", "").lower() != CONTRACT_ADDRESS.lower():
            return False
        return True


async def _call_set_stake_on_chain(code: str, amount: float, challenge: dict) -> None:
    """After negotiation completes, resolver calls setStakePerPlayer on-chain."""
    try:
        chain_id     = challenge.get("chainId", 42220)
        rpc_url      = RPC_URLS.get(chain_id, RPC_URLS[42220])
        token_symbol = challenge.get("token", "cUSD")

        DECIMALS = {"cUSD": 18, "USDC": 6, "USDT": 6, "LSK": 18}
        decimals = DECIMALS.get(token_symbol, 18)

        w3       = Web3(Web3.HTTPProvider(rpc_url))
        account  = Account.from_key(PRIVATE_KEY)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=QUIZ_HUB_ABI,
        )

        quiz_id    = Web3.keccak(text=code)
        amount_wei = int(amount * (10 ** decimals))

        tx = contract.functions.setStakePerPlayer(quiz_id, amount_wei).build_transaction({
            "from":  account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "gas":   150_000,
        })
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            logger.info("setStakePerPlayer OK  code=%s  amount=%s  tx=%s", code, amount, tx_hash.hex())
            await broadcast(code, {
                "type":   "stake_locked_on_chain",
                "amount": amount,
                "tx":     tx_hash.hex(),
            })
        else:
            logger.error("setStakePerPlayer REVERTED  code=%s", code)

    except Exception as e:
        logger.error("_call_set_stake_on_chain failed  code=%s  error=%s", code, e)


async def broadcast(code: str, payload: dict) -> None:
    sockets = connections.get(code, [])
    dead    = []
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.remove(ws)


# ─── Player Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/players/register")
async def register_player(wallet: str, username: str):
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO players (wallet_address, username)
               VALUES ($1, $2)
               ON CONFLICT (wallet_address) DO UPDATE SET username=EXCLUDED.username""",
            wallet, username,
        )
    return {"success": True}


@app.get("/api/players/{wallet}")
async def get_player(wallet: str):
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE wallet_address=$1", wallet
        )
        if not row:
            generated_username = f"User{wallet[-4:].upper()}"
            await conn.execute(
                """INSERT INTO players (wallet_address, username)
                   VALUES ($1, $2)
                   ON CONFLICT (wallet_address) DO NOTHING""",
                wallet, generated_username,
            )
            row = await conn.fetchrow(
                "SELECT * FROM players WHERE wallet_address=$1", wallet
            )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return dict(row)


# ─── Challenge Endpoints ──────────────────────────────────────────────────────
# ─── ADD THIS ENDPOINT to main.py, alongside the other stake-offer endpoints ───
# Place it after /api/challenge/{code}/pre-lobby-accept

@app.post("/api/challenge/{code}/counter")
async def send_targeted_counter(code: str, body: dict):
    """
    Creator sends a private counter-offer to a specific challenger.
    Only the targeted challenger receives the `pre_lobby_counter` WS event.

    Body: { creatorWallet, creatorName, targetWallet, amount }
    """
    code           = code.upper()
    creator_wallet = body.get("creatorWallet", "").lower()
    creator_name   = body.get("creatorName", "")
    target_wallet  = body.get("targetWallet", "").lower()
    amount         = float(body.get("amount", 0))

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    # Guard: only the creator can send a counter
    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Only the creator can send counter-offers")

    # Guard: can't counter yourself
    if target_wallet == creator_wallet:
        raise HTTPException(status_code=400, detail="Cannot counter your own wallet")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    offer = offers.get(code)
    if offer and offer.get("accepted"):
        raise HTTPException(status_code=400, detail="Stake already agreed")

    # Resolve creator display name (fall back to passed name, then truncated wallet)
    if not creator_name:
        creator_name = (
            challenge.get("creatorName")
            or challenge["players"].get(creator_wallet, {}).get("username")
            or creator_wallet[:8]
        )

    # Broadcast the counter — every socket in this room receives it,
    # but the frontend only shows the banner to the wallet matching targetWallet.
    await broadcast(code, {
        "type":         "pre_lobby_counter",
        "fromWallet":   creator_wallet,
        "fromName":     creator_name,
        "amount":       amount,
        "sentAt":       datetime.datetime.utcnow().isoformat(),
        "targetWallet": target_wallet,   # <-- always set, never null
    })

    return {"success": True, "amount": amount, "targetWallet": target_wallet}

@app.post("/api/challenge/create")
async def create_challenge(body: CreateChallengeRequest):
    # 1. Ensure player exists
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO players (wallet_address, username)
               VALUES ($1, $2) ON CONFLICT DO NOTHING""",
            body.creatorAddress.lower(), body.creatorUsername,
        )

    # 2. Generate questions via AI
    questions_data = await generate_questions(body.topic)

    # 3. Create challenge record
    code         = make_code()
    challenge_id = str(uuid.uuid4())
    creator_low  = body.creatorAddress.lower()

    challenge_obj = {
        "id":          challenge_id,
        "code":        code,
        "topic":       body.topic,
        "creator":     creator_low,
        "creatorName": body.creatorUsername,
        "stake":       body.stakeAmount,
        "token":       body.tokenSymbol,
        "chainId":     body.chainId,
        "rounds":      questions_data["rounds"],
        "status":      "waiting",
        "isPublic":    body.isPublic,
        "players": {
            creator_low: {
                "username":   body.creatorUsername,
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ai_challenges
               (id, code, creator_address, topic, stake_amount, token_symbol, chain_id, status, is_public, rounds_data)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            challenge_id, code, creator_low, body.topic, body.stakeAmount,
            body.tokenSymbol, body.chainId, "waiting", body.isPublic,
            json.dumps(questions_data),
        )
        await conn.execute(
            """INSERT INTO challenge_players (challenge_id, wallet_address, username, ready)
               VALUES ($1,$2,$3,FALSE)""",
            challenge_id, creator_low, body.creatorUsername,
        )

    challenges[code] = challenge_obj
    game_state[code] = {"answers": {}}

    # 4. Notifications — FIXED: correct if/elif structure
    if body.isPublic:
        asyncio.create_task(
            notif.notify_public_challenge(
                code,
                body.topic,
                creator_low,
                body.stakeAmount,
                body.tokenSymbol,
                creator_username=body.creatorUsername,
            )
        )
    elif body.inviteWallet:
        asyncio.create_task(
            notif.notify_friend_invite(
                code,
                body.topic,
                body.creatorUsername,
                body.inviteWallet,
                body.stakeAmount,
                body.tokenSymbol,
            )
        )

    return {"success": True, "code": code, "challenge": _safe_challenge(challenge_obj)}


# ─── Stake Offer Endpoints ────────────────────────────────────────────────────

@app.post("/api/challenge/{code}/offer")
async def submit_offer(code: str, body: StakeOfferRequest):
    code   = code.upper()
    wallet = body.walletAddress.lower()
    amount = round(body.amount, 6)

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]
    
    # 1. REMOVE THE GLOBAL LOCK
    # Instead of checking a single 'challenger', we just store this specific player's offer
    if "player_offers" not in challenge:
        challenge["player_offers"] = {}

    # Store/Update this specific player's bid
    challenge["player_offers"][wallet] = amount

    username = (
        challenge["players"].get(wallet, {}).get("username") 
        or getattr(body, "username", "Anon")
    )

    # 2. BROADCAST TO EVERYONE
    # This allows the Creator to see all offers in the UI simultaneously
    await broadcast(code, {
        "type":      "pre_lobby_offer",
        "wallet":    wallet,
        "amount":    amount,
        "username":  username,
        "sentAt":    datetime.datetime.utcnow().isoformat(),
        "isCreator": wallet == challenge["creator"].lower()
    })

    return {"success": True, "amount": amount}

@app.post("/api/challenge/{code}/accept-offer")
async def accept_offer(code: str, body: StakeOfferRequest):
    """Creator explicitly accepts the current standing offer (fallback HTTP path)."""
    code   = code.upper()
    wallet = body.walletAddress.lower()

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]
    offer     = offers.get(code)

    if not offer:
        raise HTTPException(status_code=400, detail="No open offer to accept")
    if offer["accepted"]:
        raise HTTPException(status_code=400, detail="Already accepted")
    if wallet != challenge["creator"]:
        raise HTTPException(status_code=403, detail="Only the creator can accept offers here")
    if offer["proposer"] == wallet:
        raise HTTPException(status_code=400, detail="Cannot accept your own offer — wait for the other player to respond")

    agreed_amount     = offer["current"]
    challenger        = offer.get("challenger")
    offer["accepted"] = True
    challenge["stake"]       = agreed_amount
    challenge["agreedStake"] = agreed_amount

    await _persist_agreed_stake(code, agreed_amount)
    asyncio.create_task(_call_set_stake_on_chain(code, agreed_amount, challenge))

    await broadcast(code, {
        "type":       "offer_accepted",
        "amount":     agreed_amount,
        "by":         wallet,
        "winner":     challenger,
        "challenger": challenger,
    })
    return {"success": True, "accepted": True, "amount": agreed_amount}


@app.post("/api/challenge/{code}/pre-lobby-accept")
async def pre_lobby_accept(code: str, body: dict):
    """
    Creator accepts a specific challenger's offer from the pre-lobby UI.
    Body: { creatorWallet, challengerWallet, amount }
    """
    code              = code.upper()
    creator_wallet    = body.get("creatorWallet", "").lower()
    challenger_wallet = body.get("challengerWallet", "").lower()
    amount            = float(body.get("amount", 0))

    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    if challenge["creator"].lower() != creator_wallet:
        raise HTTPException(status_code=403, detail="Only the creator can accept")

    offer = offers.get(code)
    if offer and offer.get("accepted"):
        raise HTTPException(status_code=400, detail="Already accepted")

    if offer:
        offer["accepted"] = True

    challenge["stake"]       = amount
    challenge["agreedStake"] = amount

    await _persist_agreed_stake(code, amount)
    asyncio.create_task(_call_set_stake_on_chain(code, amount, challenge))

    # Broadcast to all sockets on this challenge room
    await broadcast(code, {
        "type":       "pre_lobby_accepted",
        "amount":     amount,
        "winner":     challenger_wallet,
        "challenger": challenger_wallet,
        "by":         creator_wallet,
    })

    return {"success": True, "amount": amount}


@app.post("/api/challenge/{code}/accept-final")
async def accept_final(code: str, body: dict):
    code = code.upper()
    async with pool.acquire() as conn:
        challenge = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)

    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")

    try:
        w3      = Web3(Web3.HTTPProvider(RPC_URLS[42220]))
        account = w3.eth.account.from_key(PRIVATE_KEY)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=QUIZ_HUB_ABI,
        )
        quiz_id    = w3.keccak(text=code)
        amount_wei = w3.to_wei(float(challenge["stake_amount"]), "ether")

        tx = contract.functions.setStakePerPlayer(quiz_id, amount_wei).build_transaction({
            "from":     account.address,
            "nonce":    w3.eth.get_transaction_count(account.address),
            "gas":      200_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed   = w3.eth.account.sign_transaction(tx, private_key=account.private_key)
        tx_hash  = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash)

        if receipt.status == 1:
            await broadcast(code, {
                "type":   "stake_locked_on_chain",
                "amount": float(challenge["stake_amount"]),
            })
            return {"success": True}
        else:
            raise HTTPException(status_code=500, detail="Contract call reverted")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _persist_agreed_stake(code: str, amount: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_challenges SET stake_amount=$1 WHERE code=$2",
            amount, code,
        )


# ─── WS stake-offer relay helpers ─────────────────────────────────────────────

async def _handle_ws_stake_offer(code: str, wallet: str, msg: dict) -> None:
    amount = float(msg.get("amount", 0))
    if amount <= 0 or code not in challenges:
        return
    offer = offers.get(code)
    if not offer or offer["accepted"]:
        return
    challenge = challenges[code]
    if wallet not in challenge["players"] and wallet != challenge["creator"]:
        return
    await broadcast(code, {
        "type":     "pre_lobby_offer",
        "wallet":   wallet,
        "amount":   amount,
        "username": challenge["players"].get(wallet, {}).get("username", wallet[:8]),
        "sentAt":   datetime.datetime.utcnow().isoformat(),
        "history":  offer.get("history", []),
    })


async def _handle_ws_accept_offer(code: str, wallet: str) -> None:
    if code not in challenges:
        return
    offer = offers.get(code)
    if not offer or offer["accepted"] or offer["proposer"] == wallet:
        return
    agreed_amount     = offer["current"]
    offer["accepted"] = True
    challenges[code]["stake"] = agreed_amount
    asyncio.create_task(_persist_agreed_stake(code, agreed_amount))
    await broadcast(code, {
        "type":   "offer_accepted",
        "amount": agreed_amount,
        "by":     wallet,
        "winner": offer.get("challenger"),
    })


# ─── Profile / user endpoints ─────────────────────────────────────────────────

async def store_user_profile(profile: UserProfile):
    try:
        data = {
            "wallet_address":  profile.walletAddress,
            "x_accounts":      profile.xAccounts,
            "completed_tasks": profile.completedTasks,
            "droplist_status": profile.droplistStatus,
            "updated_at":      datetime.datetime.utcnow().isoformat(),
        }
        response = supabase.table("droplist_users").upsert(
            data, on_conflict="wallet_address"
        ).execute()
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to store user profile")
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


async def get_user_profile(wallet_address: str) -> Optional[UserProfile]:
    try:
        if not Web3.is_address(wallet_address):
            return None
        checksum_address = smart_checksum(wallet_address)
        response = supabase.table("droplist_users").select("*").eq(
            "wallet_address", checksum_address
        ).execute()
        if response.data and len(response.data) > 0:
            data = response.data[0]
            return UserProfile(
                walletAddress=data["wallet_address"],
                xAccounts=data.get("x_accounts", []),
                completedTasks=data.get("completed_tasks", []),
                droplistStatus=data.get("droplist_status", "pending"),
            )
        return None
    except Exception as e:
        logger.error("get_user_profile error: %s", e)
        return None


@app.post("/api/profile/sync")
async def sync_profile(req: SyncProfileRequest):
    try:
        wallet = normalize_db_address(req.wallet_address)
        existing = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute()
        if existing.data:
            profile_data = existing.data[0]
            profile_data["chain_type"] = "evm"
            return {"success": True, "profile": profile_data}

        username_check = supabase.table("user_profiles").select("username").eq("username", req.username).execute()
        final_username = req.username
        if username_check.data:
            final_username = f"{req.username}_{wallet[-4:]}"

        new_profile = {
            "wallet_address": wallet,
            "username":       final_username,
            "avatar_url":     req.avatar_url,
            "email":          req.email,
        }
        insert_res = supabase.table("user_profiles").insert(new_profile).execute()
        profile_data = insert_res.data[0]
        profile_data["chain_type"] = "evm"
        return {"success": True, "profile": profile_data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/users/{wallet_address}", tags=["User Management"])
async def get_user_profile_endpoint(wallet_address: str):
    try:
        profile = await get_user_profile(wallet_address)
        if not profile:
            raise HTTPException(status_code=404, detail="User profile not found")
        return profile
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get user profile: {str(e)}")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
 
def _validate_username(username: str) -> Optional[str]:
    """Returns an error message string, or None if valid."""
    if len(username) < 3:
        return "Username must be at least 3 characters"
    if len(username) > 24:
        return "Username must be 24 characters or fewer"
    if not _USERNAME_RE.match(username):
        return "Letters, numbers, and underscores only"
    return None
 
# SQL fragment — always return the same columns so callers are consistent
_PROFILE_COLS = """
    wallet_address,
    username,
    COALESCE(avatar_url, '') AS avatar_url,
    COALESCE(bio,        '') AS bio,
    COALESCE(email,      '') AS email,
    COALESCE(phone,      '') AS phone
"""
 
 
# ── GET /api/profile/{wallet} ─────────────────────────────────────────────────
 
@app.get("/api/profile/{wallet}")
async def get_profile_by_wallet(wallet: str):
    """
    Returns a player's profile by wallet address.
    Called by ProfileSettingsModal on open and by DashboardPage.
    Returns profile: null (not 404) for unknown wallets so the frontend
    can handle brand-new users gracefully.
    """
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLS} FROM players WHERE wallet_address = $1",
            wallet,
        )
    return {"success": True, "profile": dict(row) if row else None}
 
 
# ── GET /api/profile/user/{username} ─────────────────────────────────────────
 
@app.get("/api/profile/user/{username}")
async def get_profile_by_username(username: str):
    """
    Looks up a profile by username (case-insensitive).
    Used for /dashboard/<username> routes.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLS} FROM players WHERE LOWER(username) = LOWER($1)",
            username,
        )
    if not row:
        return {"success": False, "profile": None}
    return {"success": True, "profile": dict(row)}
 
 
# ── POST /api/profile/update ──────────────────────────────────────────────────
 
@app.post("/api/profile/update")
async def update_profile(body: UpdateProfileRequest):
    """
    Creates or updates a player profile.
    Called by ProfileSettingsModal on save.
    """
    wallet   = body.wallet_address.lower()
    username = body.username.strip()
 
    if not wallet:
        raise HTTPException(status_code=400, detail="wallet_address is required")
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
 
    err = _validate_username(username)
    if err:
        raise HTTPException(status_code=400, detail=err)
 
    async with pool.acquire() as conn:
        # Uniqueness check — allow the wallet to keep its own current username
        conflict = await conn.fetchrow(
            """SELECT wallet_address FROM players
               WHERE LOWER(username) = LOWER($1)
                 AND wallet_address  != $2""",
            username, wallet,
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Username is already taken")
 
        # Upsert and return the saved row
        row = await conn.fetchrow(
            f"""INSERT INTO players
                    (wallet_address, username, avatar_url, bio, email, phone, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (wallet_address) DO UPDATE
                    SET username   = EXCLUDED.username,
                        avatar_url = EXCLUDED.avatar_url,
                        bio        = EXCLUDED.bio,
                        email      = EXCLUDED.email,
                        phone      = EXCLUDED.phone,
                        updated_at = now()
                RETURNING {_PROFILE_COLS}""",
            wallet,
            username,
            body.avatar_url or "",
            body.bio        or "",
            body.email      or "",
            body.phone      or "",
        )
 
    return {"success": True, "profile": dict(row)}
 
 
# ── POST /api/profile/check-availability ─────────────────────────────────────
 
@app.post("/api/profile/check-availability")
async def check_availability(body: CheckAvailabilityRequest):
    """
    Real-time username availability check — called on input blur in
    ProfileSettingsModal. Returns { available: bool, message: str }.
    """
    value  = body.value.strip()
    wallet = body.current_wallet.lower()
 
    # Only username uniqueness is stored in players
    if body.field.lower() != "username":
        return {"available": True, "message": "Available"}
 
    err = _validate_username(value)
    if err:
        return {"available": False, "message": err}
 
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT wallet_address FROM players
               WHERE LOWER(username) = LOWER($1)
                 AND wallet_address  != $2""",
            value, wallet,
        )
 
    if row:
        return {"available": False, "message": "Username is already taken"}
    return {"available": True, "message": "Available"}
 
 
@app.post("/api/users")
async def create_user_profile_endpoint(profile: UserProfile):
    try:
        if not Web3.is_address(profile.walletAddress):
            raise HTTPException(status_code=400, detail="Invalid wallet address")
        profile.walletAddress = smart_checksum(profile.walletAddress)
        result = await store_user_profile(profile)
        return {"success": True, "message": "User profile created", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create user profile: {str(e)}")


# ─── Lobby / Challenge read endpoints ─────────────────────────────────────────

@app.get("/api/challenge/lobby")
async def get_lobby(
    limit:  int = Query(default=20, le=50),
    offset: int = Query(default=0),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM public_lobby LIMIT $1 OFFSET $2", limit, offset,
        )
    return {"success": True, "challenges": [dict(r) for r in rows]}


@app.get("/api/challenge/{code}")
async def get_challenge(code: str):
    code = code.upper()
    if code in challenges:
        return {"success": True, "challenge": _safe_challenge(challenges[code])}

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        d = dict(row)
        d["stake"]    = float(d.get("stake_amount", 0))
        d["token"]    = d.get("token_symbol", "")
        d["chainId"]  = d.get("chain_id")
        d["isPublic"] = d.get("is_public")
        d["creator"]  = d.get("creator_address", "")

        creator_row = await conn.fetchrow(
            "SELECT username FROM players WHERE wallet_address=$1", d["creator"]
        )
        d["creatorName"] = creator_row["username"] if creator_row else d["creator"][:8]

        player_rows = await conn.fetch(
            """SELECT wallet_address, username, tx_verified, ready
               FROM challenge_players WHERE challenge_id=$1""",
            d["id"],
        )
        d["players"] = {
            r["wallet_address"]: {
                "username":   r["username"],
                "points":     0,
                "ready":      r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }

    return {"success": True, "challenge": d}


@app.post("/api/challenge/{code}/join")
async def join_challenge(code: str, body: JoinChallengeRequest):
    code   = code.upper()
    joiner = body.walletAddress.lower()

    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")

        d           = dict(row)
        rounds_data = d.get("rounds_data") or {}
        if isinstance(rounds_data, str):
            rounds_data = json.loads(rounds_data)

        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                "SELECT wallet_address, username, tx_verified, ready FROM challenge_players WHERE challenge_id=$1",
                d["id"],
            )

        players_dict = {
            r["wallet_address"]: {
                "username":   r["username"],
                "points":     0,
                "ready":      r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }

        challenges[code] = {
            "id":          str(d["id"]),
            "code":        code,
            "topic":       d["topic"],
            "creator":     d["creator_address"],
            "creatorName": d.get("creator_name", ""),
            "stake":       float(d["stake_amount"]),
            "token":       d["token_symbol"],
            "chainId":     d["chain_id"],
            "rounds":      rounds_data.get("rounds", []),
            "status":      d["status"],
            "isPublic":    d["is_public"],
            "players":     players_dict,
        }
        game_state[code] = {"answers": {}}

    challenge = challenges[code]

    if challenge["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Challenge is not open")
    if len(challenge["players"]) >= 2:
        raise HTTPException(status_code=400, detail="Challenge is already full")
    if joiner in challenge["players"]:
        return {"success": True, "challenge": _safe_challenge(challenge)}

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO players (wallet_address, username) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            joiner, body.username,
        )
        await conn.execute(
            """INSERT INTO challenge_players
                (challenge_id, wallet_address, username, tx_hash, tx_verified)
               VALUES ((SELECT id FROM ai_challenges WHERE code=$1), $2, $3, $4, TRUE)
               ON CONFLICT (challenge_id, wallet_address)
               DO UPDATE SET tx_hash=$4, tx_verified=TRUE""",
            code, joiner, body.username, body.txHash,
        )

    challenge["players"][joiner] = {
        "username":   body.username,
        "points":     0,
        "ready":      False,
        "txVerified": True,
    }

    asyncio.create_task(
        notif.notify_player_joined(code, body.username, challenge["creator"])
    )

    await broadcast(code, {
        "type":   "player_joined",
        "player": {"walletAddress": joiner, "username": body.username},
    })
    await _maybe_auto_start(code)
    return {"success": True, "challenge": _safe_challenge(challenge)}


@app.post("/api/challenge/{code}/rematch")
async def request_rematch(code: str, body: RematachRequest):
    code      = code.upper()
    requester = body.requesterWallet.lower()

    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)
        if not row:
            raise HTTPException(status_code=404, detail="Original challenge not found")
        orig = dict(row)
        topic, stake, token, chain_id, orig_id = (
            orig["topic"], float(orig["stake_amount"]),
            orig["token_symbol"], orig["chain_id"], str(orig["id"]),
        )
        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                "SELECT wallet_address, username FROM challenge_players WHERE challenge_id=$1",
                orig["id"],
            )
        players = {r["wallet_address"]: r["username"] for r in player_rows}
    else:
        c        = challenges[code]
        topic    = c["topic"]
        stake    = c["stake"]
        token    = c["token"]
        chain_id = c["chainId"]
        orig_id  = c["id"]
        players  = {w: p["username"] for w, p in c["players"].items()}

    if requester not in players:
        raise HTTPException(status_code=403, detail="You were not part of this challenge")

    requester_username = players[requester]
    opponent_wallet    = next((w for w in players if w != requester), None)
    if not opponent_wallet:
        raise HTTPException(status_code=400, detail="Cannot find opponent")

    questions_data = await generate_questions(topic)
    new_code       = make_code()
    new_id         = str(uuid.uuid4())

    new_challenge = {
        "id":          new_id,
        "code":        new_code,
        "topic":       topic,
        "creator":     requester,
        "creatorName": requester_username,
        "stake":       stake,
        "token":       token,
        "chainId":     chain_id,
        "rounds":      questions_data["rounds"],
        "status":      "waiting",
        "isPublic":    False,
        "players": {
            requester: {
                "username":   requester_username,
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ai_challenges
               (id, code, creator_address, topic, stake_amount, token_symbol,
                chain_id, status, is_public, rounds_data, rematch_of)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'waiting',FALSE,$8,$9)""",
            new_id, new_code, requester, topic, stake, token,
            chain_id, json.dumps(questions_data), orig_id,
        )
        await conn.execute(
            "INSERT INTO challenge_players (challenge_id, wallet_address, username) VALUES ($1,$2,$3)",
            new_id, requester, requester_username,
        )

    challenges[new_code] = new_challenge
    game_state[new_code] = {"answers": {}}

    asyncio.create_task(
        notif.notify_rematch_request(new_code, requester_username, opponent_wallet)
    )

    return {"success": True, "newCode": new_code, "challenge": _safe_challenge(new_challenge)}


@app.post("/api/challenge/{code}/sync-stake")
async def sync_stake(code: str, body: dict):
    code   = code.upper()
    wallet = body.get("walletAddress", "").lower()

    if not wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]
    if wallet not in challenge["players"]:
        raise HTTPException(status_code=403, detail="You are not in this challenge")
    if challenge["players"][wallet].get("txVerified"):
        return {"success": True, "alreadyVerified": True}

    GET_QUIZ_ABI = [{
        "inputs": [{"internalType": "bytes32", "name": "quizId", "type": "bytes32"}],
        "name": "getQuiz",
        "outputs": [{
            "components": [
                {"internalType": "bytes32",  "name": "id",             "type": "bytes32"},
                {"internalType": "address",  "name": "token",          "type": "address"},
                {"internalType": "uint256",  "name": "stakePerPlayer", "type": "uint256"},
                {"internalType": "uint256",  "name": "totalStaked",    "type": "uint256"},
                {"internalType": "address",  "name": "player1",        "type": "address"},
                {"internalType": "address",  "name": "player2",        "type": "address"},
                {"internalType": "address",  "name": "winner",         "type": "address"},
                {"internalType": "bool",     "name": "resolved",       "type": "bool"},
                {"internalType": "bool",     "name": "rewardClaimed",  "type": "bool"},
                {"internalType": "uint256",  "name": "createdAt",      "type": "uint256"},
            ],
            "internalType": "struct QuizHub.Quiz",
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    }]

    try:
        w3       = Web3(Web3.HTTPProvider(RPC_URLS[42220]))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=GET_QUIZ_ABI,
        )
        quiz_id_bytes = Web3.keccak(text=code)
        quiz          = contract.functions.getQuiz(quiz_id_bytes).call()

        stake_per_player = quiz[2]
        total_staked     = quiz[3]
        player1          = quiz[4].lower()
        player2          = quiz[5].lower()

        if wallet == player1:
            has_staked = total_staked >= stake_per_player
        elif wallet == player2:
            has_staked = total_staked >= stake_per_player * 2
        else:
            has_staked = False

    except Exception as e:
        logger.error("sync-stake on-chain check failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach contract")

    if not has_staked:
        return {"success": False, "verified": False, "message": "No stake found on-chain"}

    challenge["players"][wallet]["txVerified"] = True

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_players SET tx_verified=TRUE
               WHERE challenge_id=(SELECT id FROM ai_challenges WHERE code=$1)
                 AND wallet_address=$2""",
            code, wallet,
        )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})
    await _maybe_auto_start(code)

    return {"success": True, "verified": True}


@app.get("/api/challenge/{wallet}/history")
async def challenge_history(wallet: str, limit: int = Query(default=10, le=50)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.code, c.topic, c.stake_amount, c.token_symbol,
                      c.status, c.winner_address, c.created_at, c.finished_at
               FROM ai_challenges c
               JOIN challenge_players cp ON cp.challenge_id = c.id
               WHERE cp.wallet_address=$1
               ORDER BY c.created_at DESC LIMIT $2""",
            wallet.lower(), limit,
        )
    return {"success": True, "history": [dict(r) for r in rows]}


@app.get("/api/challenge/{wallet}/pending-claims")
async def get_pending_claims(wallet: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT code, topic, stake_amount * 2 AS win_amount, token_symbol, chain_id
               FROM ai_challenges
               WHERE winner_address=$1 AND claimed=FALSE AND status='finished'""",
            wallet.lower(),
        )
    return {"success": True, "claims": [dict(r) for r in rows]}


@app.post("/api/challenge/claim")
async def claim_win(body: ClaimRequest):
    code   = body.code.upper()
    wallet = body.walletAddress.lower()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)

    if not row:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if row["winner_address"] != wallet:
        raise HTTPException(status_code=403, detail="You are not the winner")
    if row["claimed"]:
        raise HTTPException(status_code=400, detail="Already claimed")

    async with pool.acquire() as conn:
        await conn.execute("UPDATE ai_challenges SET claimed=TRUE WHERE code=$1", code)
        await conn.execute(
            "UPDATE players SET total_earned=total_earned+$1 WHERE wallet_address=$2",
            float(row["stake_amount"]) * 2, wallet,
        )

    return {
        "success": True,
        "message": "Reward marked for payout",
        "amount":  float(row["stake_amount"]) * 2,
    }


# ─── Notification Endpoints ───────────────────────────────────────────────────

@app.get("/api/notifications/{wallet}")
async def get_notifications(wallet: str, limit: int = Query(default=20, le=50)):
    items = await notif.get_unread(wallet, limit)
    return {"success": True, "notifications": items}


@app.get("/api/notifications/{wallet}/count")
async def get_unread_count(wallet: str):
    count = await notif.unread_count(wallet)
    return {"success": True, "unread": count}


@app.post("/api/notifications/{wallet}/read/{notif_id}")
async def mark_read(wallet: str, notif_id: str):
    await notif.mark_read(wallet, notif_id)
    return {"success": True}


@app.post("/api/notifications/{wallet}/read-all")
async def mark_all_read(wallet: str):
    await notif.mark_all_read(wallet)
    return {"success": True}


# ─── WebSocket: Game ──────────────────────────────────────────────────────────

@app.websocket("/ws/challenge/{code}")
async def challenge_socket(ws: WebSocket, code: str):
    code = code.upper()
    await ws.accept()
    connections.setdefault(code, []).append(ws)

    if code in challenges:
        await ws.send_json({
            "type":      "state_sync",
            "challenge": _safe_challenge(challenges[code]),
        })

    try:
        async for msg in ws.iter_json():
            m_type = msg.get("type")
            wallet = msg.get("walletAddress", "").lower()

            if m_type == "stake_confirmed":
                await _handle_stake_confirmed(code, wallet, msg.get("txHash", ""))
            elif m_type == "ready":
                await _handle_ready(code, wallet)
            elif m_type == "submit_answer":
                _handle_submit_answer(code, wallet, msg)
            elif m_type == "stake_offer":
                await _handle_ws_stake_offer(code, wallet, msg)
            elif m_type == "accept_offer":
                await _handle_ws_accept_offer(code, wallet)
            elif m_type == "chat":
                await broadcast(code, {
                    "type":      "chat",
                    "sender":    msg.get("username"),
                    "wallet":    wallet,
                    "text":      msg.get("text"),
                    "timestamp": asyncio.get_event_loop().time(),
                })

    except WebSocketDisconnect:
        sockets = connections.get(code, [])
        if ws in sockets:
            sockets.remove(ws)


async def _handle_stake_confirmed(code: str, wallet: str, tx_hash: str) -> None:
    if code not in challenges:
        return

    challenge = challenges[code]

    if wallet not in challenge["players"]:
        logger.warning("stake_confirmed from unknown wallet %s for code %s — ignoring", wallet, code)
        return

    if challenge["players"][wallet].get("txVerified"):
        await broadcast(code, {"type": "stake_verified", "wallet": wallet})
        return

    challenge["players"][wallet]["txVerified"] = True
    challenge["players"][wallet]["txHash"]     = tx_hash

    if tx_hash not in ("auto-sync", "sync-recovery"):
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_players SET tx_hash=$1, tx_verified=TRUE
                   WHERE challenge_id=(SELECT id FROM ai_challenges WHERE code=$2)
                     AND wallet_address=$3""",
                tx_hash, code, wallet,
            )
    else:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_players SET tx_verified=TRUE
                   WHERE challenge_id=(SELECT id FROM ai_challenges WHERE code=$1)
                     AND wallet_address=$2""",
                code, wallet,
            )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})


async def _handle_ready(code: str, wallet: str) -> None:
    if code not in challenges or wallet not in challenges[code]["players"]:
        return
    challenge = challenges[code]
    if not challenge["players"][wallet].get("txVerified"):
        return
    challenge["players"][wallet]["ready"] = True
    await broadcast(code, {"type": "player_ready", "wallet": wallet})
    await _maybe_auto_start(code)


def _handle_submit_answer(code: str, wallet: str, msg: dict) -> None:
    if code not in game_state:
        return
    r_idx = msg.get("roundIndex")
    q_idx = msg.get("questionIndex")
    key   = f"{r_idx}_{q_idx}"
    state = game_state[code]

    # Initialize the question key if it doesn't exist
    if key not in state["answers"]:
        state["answers"][key] = {}

    # Overwrite the answer (this allows the user to change their mind)
    state["answers"][key][wallet] = {
        "answerId":  msg.get("answerId"),
        "timeTaken": msg.get("timeTaken", 0),
    }


async def _maybe_auto_start(code: str) -> None:
    if code not in challenges:
        return
    c = challenges[code]
    if (
        len(c["players"]) == 2
        and all(p.get("txVerified") for p in c["players"].values())
        and all(p.get("ready")      for p in c["players"].values())
        and c["status"] == "waiting"
    ):
        wallets = list(c["players"].keys())
        asyncio.create_task(notif.notify_game_starting(code, wallets))
        asyncio.create_task(
            run_game_loop(code, challenges, game_state, pool, broadcast, notif)
        )


# ─── WebSocket: Live Notifications ───────────────────────────────────────────

@app.websocket("/ws/notify/{wallet}")
async def notify_socket(ws: WebSocket, wallet: str):
    wallet = wallet.lower()
    await ws.accept()
    notify_conn.setdefault(wallet, []).append(ws)

    try:
        count = await notif.unread_count(wallet)
        await ws.send_json({"type": "unread_count", "count": count})
    except (WebSocketDisconnect, Exception):
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)
        return

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _safe_challenge(c: dict) -> dict:
    import copy
    safe       = copy.deepcopy(c)
    safe["player_offers"] = c.get("player_offers", {})
    safe_rounds = strip_answers({"rounds": safe.get("rounds", [])})
    safe["rounds"] = safe_rounds["rounds"]
    if "agreedStake" not in safe:
        safe["agreedStake"] = None
    return safe


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)